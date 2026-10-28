#!/usr/bin/env python3
"""Fit the quality weights against external rankings. Rules: docs/promotion-strength-v2.md

The weights, K_SCALE_Z and the two shrinkage constants were priors, never fitted. This
searches them against the two sources in `data/rankings/`, and because there are only 9
labelled majors and 13 Fight Matrix promotions, it does the search CROSS-SOURCE: fit on one,
report on the other. A gain that survives that is a gain; one that does not is overfitting to
thirteen numbers.

No model re-run is needed. `norm` is a monotone function of the composite and both criteria
are rank-based, so the composite is rebuilt from the `z` values already in the rows.

  python3 scripts/tune_promotion_strength.py [--iters 4000]
"""
import argparse, json, collections, random, sys, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SIG = ["intake", "elite", "title", "consistency", "card", "match", "ops", "dist"]
BASE = {"intake": 0.099, "elite": 0.120, "title": 0.152, "consistency": 0.107,
        "card": 0.201, "match": 0.214, "dist": 0.081, "ops": 0.027}
sys.path.insert(0, str(ROOT / "scripts"))
from validate_promotion_strength import auc, spearman, latest      # noqa: E402


def composite(o, w, ks, mc, mt):
    z = o["z"]
    den = sum(w[k] for k in SIG if z.get(k) is not None)
    if not den: return None
    q = sum(w[k] * z[k] for k in SIG if z.get(k) is not None) / den
    b = o["raw"]["level_bouts"]; t = (o.get("activity") or {}).get("tenure_months", 0.0)
    return (q + ks * o["scale"]) * (b / (b + mc)) * (t / (t + mt) if t + mt else 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-08-23")
    ap.add_argument("--sport", default="MMA")
    ap.add_argument("--active-within", type=int, default=365)
    ap.add_argument("--iters", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()

    labels = [json.loads(l) for l in open(DATA / "rankings/tapology_major_orgs.jsonl")]
    majors = {o["promotion_id"] for o in labels if o["status"] == "major" and a.sport in o["sports"]}
    skip = {o["promotion_id"] for o in labels if o["status"] != "major"}
    fm = [json.loads(l) for l in open(DATA / "rankings/fightmatrix_promotions_2026-08-23.jsonl")]
    rows = latest(a.date, a.sport)

    def idle(o):
        act = o.get("activity") or {}
        if "days_since_last_event" not in act: return None
        return act["days_since_last_event"] + (dt.date.fromisoformat(a.date)
                                               - dt.date.fromisoformat(o["date"])).days
    live = {p: o for p, o in rows.items()
            if (idle(o) is None or idle(o) <= a.active_within) and p not in skip}
    have = [r for r in fm if r["promotion_id"] in rows]

    def major_auc(P):
        c = {p: composite(o, *P) for p, o in live.items()}
        pos = [v for p, v in c.items() if p in majors and v is not None]
        neg = [v for p, v in c.items() if p not in majors and v is not None]
        return auc(pos, neg)

    def fm_rho(P):
        ours = [composite(rows[r["promotion_id"]], *P) for r in have]
        return sum(spearman(ours, [s * r[k] for r in have]) for k, s in
                   (("avg_rank", -1), ("point_total", 1), ("point_avg", 1),
                    ("ranked_fighters", 1))) / 4.0

    def rnd(rng):
        w = {k: rng.random() ** 1.5 for k in SIG}
        tot = sum(w.values())
        return ({k: v / tot for k, v in w.items()}, rng.choice([0.0, 0.15, 0.35, 0.6, 1.0]),
                rng.choice([0, 5, 10, 20, 40]), rng.choice([0, 6, 12, 24, 36]))

    base = (BASE, 0.42, 14, 7)
    print(f"{a.sport} {a.date}: {len(live)} live promotions, {len(majors)} majors, "
          f"{len(have)} Fight Matrix rows\n")
    print(f"  {'setting':<34}{'MajorOrg AUC':>14}{'FM Spearman':>13}")
    print(f"  {'shipped':<34}{major_auc(base):>14.4f}{fm_rho(base):>13.3f}")

    rng = random.Random(a.seed)
    cands = [rnd(rng) for _ in range(a.iters)]
    for name, obj, other in (("fit on Fight Matrix", fm_rho, major_auc),
                             ("fit on Major-Org", major_auc, fm_rho)):
        best = max(cands, key=obj)
        print(f"  {name + ' -> held-out':<34}{(major_auc(best) if other is major_auc else obj(best)):>14.4f}"
              f"{(fm_rho(best) if other is fm_rho else obj(best)):>13.3f}")
    both = max(cands, key=lambda P: major_auc(P) + fm_rho(P))
    print(f"  {'fit on both':<34}{major_auc(both):>14.4f}{fm_rho(both):>13.3f}")
    w, ks, mc, mt = both
    print("\n  weights fitted on both sources (shipped in brackets):")
    for k in sorted(SIG, key=lambda k: -w[k]):
        print(f"    {k:<14}{w[k]:.3f}   [{BASE[k]:.2f}]")
    print(f"    K_SCALE_Z     {ks}   [0.35]\n    M_CELL        {mc}   [10]"
          f"\n    M_TENURE      {mt}   [12]")

    # With 9 positives and 13 ranked promotions the weights are barely identified: many very
    # different vectors score the same, and an argmax lands in a corner. So report the
    # ENSEMBLE of the top 1% of candidates -- a weight whose good solutions agree is
    # determined by the data, one that ranges over the whole interval is not.
    scored = sorted(cands, key=lambda P: major_auc(P) + fm_rho(P), reverse=True)
    elite = scored[:max(10, len(scored) // 100)]
    print(f"\n  top {len(elite)} solutions of {len(cands)}, by weight:")
    print(f"    {'signal':<14}{'mean':>7}{'sd':>7}{'min':>7}{'max':>7}   [shipped]")
    ens = {}
    for k in SIG:
        v = [P[0][k] for P in elite]
        ens[k] = sum(v) / len(v)
        print(f"    {k:<14}{ens[k]:>7.3f}{(sum((x-ens[k])**2 for x in v)/len(v))**0.5:>7.3f}"
              f"{min(v):>7.3f}{max(v):>7.3f}   [{BASE[k]:.2f}]")
    for lab, idx in (("K_SCALE_Z", 1), ("M_CELL", 2), ("M_TENURE", 3)):
        v = [P[idx] for P in elite]
        print(f"    {lab:<14}{sum(v)/len(v):>7.2f}{'':>7}{min(v):>7}{max(v):>7}")
    ks_e = sum(P[1] for P in elite) / len(elite)
    mc_e = sum(P[2] for P in elite) / len(elite)
    mt_e = sum(P[3] for P in elite) / len(elite)
    ENS = (ens, ks_e, mc_e, mt_e)
    half = ({k: 0.5 * ens[k] + 0.5 * BASE[k] for k in SIG},
            0.5 * ks_e + 0.5 * 0.42, 0.5 * mc_e + 0.5 * 14, 0.5 * mt_e + 0.5 * 7)
    print(f"\n  {'setting':<34}{'MajorOrg AUC':>14}{'FM Spearman':>13}")
    print(f"  {'shipped':<34}{major_auc(base):>14.4f}{fm_rho(base):>13.3f}")
    print(f"  {'argmax (both)':<34}{major_auc(both):>14.4f}{fm_rho(both):>13.3f}")
    print(f"  {'ensemble mean of top 1%':<34}{major_auc(ENS):>14.4f}{fm_rho(ENS):>13.3f}")
    print(f"  {'halfway to the prior':<34}{major_auc(half):>14.4f}{fm_rho(half):>13.3f}")

    # how much of the spread is luck: the same search on shuffled labels
    sh = list(live)
    rng.shuffle(sh)
    fake = set(sh[:len(majors)])
    def fake_auc(P):
        c = {p: composite(o, *P) for p, o in live.items()}
        return auc([v for p, v in c.items() if p in fake and v is not None],
                   [v for p, v in c.items() if p not in fake and v is not None])
    print(f"\n  sanity: best AUC reachable on a RANDOM label of the same size: "
          f"{max(fake_auc(P) for P in cands[:800]):.4f} (real: {major_auc(both):.4f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
