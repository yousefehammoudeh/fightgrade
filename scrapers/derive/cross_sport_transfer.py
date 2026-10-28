#!/usr/bin/env python3
"""What a rating point in one combat sport is worth in another. Rules: docs/cross-sport-transfer.md

  kappa(a -> b)  =  rating points gained in b per rating point earned in a

Estimated from records only -- never from a rating -- because this feeds the rating engine,
the same circularity rule promotion strength works under. Two steps:

  1. c_s   within a sport, how many rating points one unit of record quality q is worth,
           from the empirical slope of logit(win) against the quality gap.
  2. m(a->b)  across sports, rating points in b per unit of q in a, after subtracting the
           win probability the fighter's own quality gap inside b already predicts.
           kappa = m(a->b) / c_a.

Outputs
  data/cross_sport_transfer.json   c_s, qbar and kappa per ordered sport pair

  python3 scrapers/derive/cross_sport_transfer.py [--validate]
"""
import argparse, bisect, collections, json, math, pickle, sys, datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from promotion_strength import CACHE, DATA, build_cache, ALPHA, K_EXP        # noqa: E402

LOG10   = 400 / math.log(10)   # Elo rating points per unit of log-odds
MIN_A   = 3                    # bouts in the source sport before it can inform anything
MAX_B   = 4                    # bouts already had in the destination sport (near-debut only)
MIN_FIT = 3000                 # bouts before a sport gets its own within-sport curve
MIN_PAIR = 60                  # observations before a pair gets its own kappa
CUT     = dt.date(2019, 1, 1)  # train/test split for --validate


def load():
    C = pickle.load(open(CACHE, "rb")) if CACHE.exists() else build_cache()
    B = [r for r in C["B"] if r[3] and len(r[10]) == 2]          # professional, two-sided
    B.sort(key=lambda r: r[2])
    hist, qbar, sports, within = (collections.defaultdict(list), collections.defaultdict(list),
                                  collections.defaultdict(set), collections.defaultdict(list))
    for r in B:
        for i, (slug, res, ra, nat) in enumerate(r[10]):
            if res not in ("W", "L"): continue
            n_, wr = r[12][i]; on_, owr = r[12][1 - i]
            q = ALPHA * wr + (1 - ALPHA) * n_ / (n_ + K_EXP)
            oq = ALPHA * owr + (1 - ALPHA) * on_ / (on_ + K_EXP)
            qbar[r[1]].append(q)
            within[r[1]].append((q - oq, 1 if res == "W" else 0))
            if slug:
                hist[(slug, r[1])].append((r[2], q, res)); sports[slug].add(r[1])
    return B, hist, {s: sum(v) / len(v) for s, v in qbar.items()}, sports, within


def logit_slope(pairs, lo=-0.5, hi=0.5, nb=12, nmin=40):
    """Weighted least squares of the empirical logit against x. Binned rather than fitted by
    maximum likelihood: with 300k bouts per sport the bins are dense and it stays inspectable."""
    cell = collections.defaultdict(lambda: [0, 0])
    for x, w in pairs:
        if x < lo or x > hi: continue
        c = cell[min(nb - 1, int((x - lo) / (hi - lo) * nb))]; c[0] += w; c[1] += 1
    pts = []
    for b, (a, n) in cell.items():
        if n < nmin: continue
        p = min(max(a / n, 0.02), 0.98)
        pts.append((lo + (b + 0.5) * (hi - lo) / nb, math.log(p / (1 - p)), n))
    if len(pts) < 4: return None
    sw = sum(p[2] for p in pts)
    mx = sum(p[0] * p[2] for p in pts) / sw; my = sum(p[1] * p[2] for p in pts) / sw
    den = sum(p[2] * (p[0] - mx) ** 2 for p in pts)
    if not den: return None
    b1 = sum(p[2] * (p[0] - mx) * (p[1] - my) for p in pts) / den
    return my - b1 * mx, b1


def observations(B, hist, qbar, sports, curve):
    """One row per (crossover fighter, bout): their quality in the old sport, and how far the
    result beat what their own quality gap inside the new sport predicted."""
    out = []
    for r in B:
        sp, d = r[1], r[2]
        if sp not in curve: continue
        b0, b1 = curve[sp]
        for i in (0, 1):
            slug, res = r[10][i][0], r[10][i][1]
            if not slug or res not in ("W", "L"): continue
            if len([x for x in hist.get((slug, sp), ()) if x[0] < d]) > MAX_B: continue
            exp = 1 / (1 + math.exp(-(b0 + b1 * max(-0.6, min(0.6, r[12][i][1] - r[12][1 - i][1])))))
            for s2 in sports[slug]:
                if s2 == sp or s2 not in qbar: continue
                pr = [x for x in hist[(slug, s2)] if x[0] < d]
                if len(pr) < MIN_A: continue
                out.append(dict(d=d, pair=(s2, sp), x=pr[-1][1] - qbar[s2],
                                y=1 if res == "W" else 0, exp=exp))
    return out


def slope(rows, key="x"):
    if len(rows) < MIN_PAIR: return None
    mx = sum(p[key] for p in rows) / len(rows)
    my = sum(p["y"] - p["exp"] for p in rows) / len(rows)
    den = sum((p[key] - mx) ** 2 for p in rows)
    if not den: return None
    return sum((p[key] - mx) * ((p["y"] - p["exp"]) - my) for p in rows) / den


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", action="store_true")
    a = ap.parse_args()

    B, hist, qbar, sports, within = load()
    curve = {s: logit_slope(v) for s, v in within.items() if len(v) >= MIN_FIT}
    curve = {s: v for s, v in curve.items() if v}
    c_s = {s: v[1] * LOG10 for s, v in curve.items()}
    print(f"  {'sport':<24}{'bouts':>10}{'logit/q':>10}{'rating pts per q':>18}")
    for s in sorted(c_s, key=lambda s: -c_s[s]):
        print(f"  {s:<24}{len(within[s]):>10,}{curve[s][1]:>10.2f}{c_s[s]:>18.0f}")

    obs = observations(B, hist, qbar, sports, curve)
    byp = collections.defaultdict(list)
    for z in obs: byp[z["pair"]].append(z)
    kappa = {}
    print(f"\n  {'A -> B':<34}{'obs':>7}{'pts in B / q_A':>16}{'kappa':>8}")
    for (a_, b_), rows in sorted(byp.items(), key=lambda kv: -len(kv[1])):
        sl = slope(rows)
        if sl is None or a_ not in c_s: continue
        m = sl * 4 * LOG10                      # residual slope -> logit slope -> rating pts
        kappa[f"{a_}->{b_}"] = round(m / c_s[a_], 3)
        print(f"  {a_ + ' -> ' + b_:<34}{len(rows):>7}{m:>16.0f}{m / c_s[a_]:>8.2f}")

    (DATA / "cross_sport_transfer.json").write_text(json.dumps(
        {"c_s": {k: round(v, 1) for k, v in c_s.items()},
         "qbar": {k: round(v, 4) for k, v in qbar.items() if k in c_s},
         "kappa": kappa, "n_obs": {f"{a_}->{b_}": len(v) for (a_, b_), v in byp.items()}},
        indent=1))
    print(f"\nwrote data/cross_sport_transfer.json  ({len(kappa)} ordered pairs)")

    if a.validate:
        tr = [z for z in obs if z["d"] < CUT.toordinal()]
        te = [z for z in obs if z["d"] >= CUT.toordinal()]
        K = {}
        for p, sub in collections.defaultdict(list, {k: [z for z in tr if z["pair"] == k]
                                                     for k in byp}).items():
            s_ = slope(sub)
            if s_ is not None: K[p] = s_
        def score(use_kappa):
            br = ll = n = tot = 0
            for z in te:
                if use_kappa and z["pair"] not in K: continue
                p = z["exp"] if not use_kappa else 1 / (1 + math.exp(
                    -(math.log(z["exp"] / (1 - z["exp"])) + K[z["pair"]] * 4 * z["x"])))
                p = min(max(p, 0.02), 0.98)
                br += (p - z["y"]) ** 2; ll += -(z["y"] * math.log(p) + (1 - z["y"]) * math.log(1 - p))
                n += 1; tot += p
            return n, br / n, ll / n, tot / n
        print(f"\n  out of sample: train {len(tr):,} (< {CUT}), test {len(te):,}")
        print(f"  {'model':<44}{'n':>7}{'Brier':>9}{'log loss':>10}{'mean P':>9}")
        for lab, uk in (("destination record only", False), ("+ kappa", True)):
            n, br, l_, mp = score(uk)
            print(f"  {lab:<44}{n:>7}{br:>9.4f}{l_:>10.4f}{mp:>9.1%}")
        print(f"  {'(actual win rate in test)':<44}{'':>7}{'':>9}{'':>10}"
              f"{sum(z['y'] for z in te) / len(te):>9.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
