#!/usr/bin/env python3
"""Validate promotion strength against external rankings. Rules: docs/promotion-strength-v2.md

Two independent sources, and they disagree with each other, which is the point:

  data/rankings/tapology_major_orgs.jsonl   Tapology's Major Org label -- an editorial tier,
      read off the Major-Org event filter for MMA and Boxing on 2026-08-28. It is a
      PROMOTION-level flag, not per sport. Bellator and Invicta are marked
      `unknown_inactive`: they were previously listed but run no events now, so a schedule
      cannot confirm them and they are excluded from the test rather than scored as
      negatives.

  data/rankings/fightmatrix_promotions_2026-08-23.jsonl   Fight Matrix, release #1038,
      four orderings of the same 13 promotions (>= 100 ranked fighters): average fighter
      rank, total rating points, average rating points, and count of ranked fighters. All
      four are aggregates of Fight Matrix's own fighter ratings, so they are Elo-derived and
      cannot be an input -- only a check.

  python3 scripts/validate_promotion_strength.py [--date 2026-08-23] [--sport MMA]
"""
import argparse, json, collections, sys, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SIG = ["intake", "elite", "title", "consistency", "card", "match", "ops", "dist"]


def auc(pos, neg):
    """Mann-Whitney: P(a random positive outranks a random negative), ties at 0.5."""
    if not pos or not neg: return float("nan")
    xs = sorted([(v, 1) for v in pos] + [(v, 0) for v in neg])
    r, i = {}, 0
    while i < len(xs):
        j = i
        while j < len(xs) and xs[j][0] == xs[i][0]: j += 1
        for k in range(i, j): r[k] = (i + j + 1) / 2.0
        i = j
    s = sum(r[k] for k, (v, lab) in enumerate(xs) if lab == 1)
    return (s - len(pos) * (len(pos) + 1) / 2.0) / (len(pos) * len(neg))


def spearman(a, b):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v); i = 0
        while i < len(order):
            j = i
            while j < len(order) and v[order[j]] == v[order[i]]: j += 1
            for k in range(i, j): r[order[k]] = (i + j - 1) / 2.0
            i = j
        return r
    ra, rb = rank(a), rank(b); n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return num / den if den else float("nan")


def latest(date, sport, level="pro"):
    best = {}
    for line in open(DATA / "promotion_strength_events.jsonl"):
        o = json.loads(line)
        if o["level"] != level or o["sport"] != sport or o["date"] > date: continue
        k = o["promotion_id"]
        if k not in best or o["date"] > best[k]["date"]: best[k] = o
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-08-23")
    ap.add_argument("--sport", default="MMA")
    ap.add_argument("--active-within", type=int, default=365)
    a = ap.parse_args()

    names = {}
    for f in ("promotions.jsonl", "promoters_freetext.jsonl"):
        for line in open(DATA / f):
            o = json.loads(line); names[o["id"]] = o["name"]
    labels = [json.loads(l) for l in open(DATA / "rankings/tapology_major_orgs.jsonl")]
    majors = {o["promotion_id"] for o in labels
              if o["status"] == "major" and a.sport in o["sports"]}
    skip = {o["promotion_id"] for o in labels if o["status"] != "major"}
    fm = [json.loads(l) for l in
          open(DATA / f"rankings/fightmatrix_promotions_2026-08-23.jsonl")]

    rows = latest(a.date, a.sport)
    def idle(o):
        act = o.get("activity") or {}
        if "days_since_last_event" not in act: return None
        return act["days_since_last_event"] + (dt.date.fromisoformat(a.date)
                                               - dt.date.fromisoformat(o["date"])).days
    live = {p: o for p, o in rows.items()
            if (idle(o) is None or idle(o) <= a.active_within) and p not in skip}
    pos = [o for p, o in live.items() if p in majors]
    neg = [o for p, o in live.items() if p not in majors]
    print(f"{a.sport}, {a.date}, active within {a.active_within}d: {len(live):,} promotions, "
          f"{len(pos)} of {len(majors)} labelled majors present, {len(skip)} excluded as "
          f"unknown/inactive")

    print(f"\n  {'field':<16}{'AUC':>8}")
    for lab, get in (("norm", lambda o: o["norm"]), ("quality", lambda o: o["quality"]),
                     ("scale", lambda o: o["scale"])):
        print(f"  {lab:<16}{auc([get(o) for o in pos], [get(o) for o in neg]):>8.4f}")
    for k in SIG:
        p_ = [o["z"][k] for o in pos if o["z"].get(k) is not None]
        n_ = [o["z"][k] for o in neg if o["z"].get(k) is not None]
        print(f"  {'z.' + k:<16}{auc(p_, n_):>8.4f}   ({len(p_)}/{len(pos)} majors have it)")

    order = sorted(live.values(), key=lambda o: -o["norm"])
    rank = {o["promotion_id"]: i + 1 for i, o in enumerate(order)}
    print(f"\n  {'labelled major':<34}{'our rank':>9}{'norm':>8}")
    for p in sorted(majors, key=lambda p: rank.get(p, 1 << 30)):
        if p in rank: print(f"  {names.get(p, p)[:33]:<34}{rank[p]:>9}{live[p]['norm']:>8.3f}")
        else: print(f"  {names.get(p, p)[:33]:<34}{'--':>9}   not scored / inactive")
    top = order[:12]
    print(f"\n  precision@12 = {sum(1 for o in top if o['promotion_id'] in majors)}/12; "
          f"unlabelled promotions in our top 12:")
    for o in top:
        if o["promotion_id"] not in majors:
            print(f"    {rank[o['promotion_id']]:>3}. "
                  f"{names.get(o['promotion_id'], '?')[:40]:<42}{o['norm']:.3f}")

    print(f"\n  Fight Matrix agreement (Spearman over the {len(fm)} promotions it ranks):")
    have = [r for r in fm if r["promotion_id"] in rows]
    ours = [rows[r["promotion_id"]]["norm"] for r in have]
    for lab, key, sign in (("average fighter rank", "avg_rank", -1),
                           ("total rating points", "point_total", 1),
                           ("average rating points", "point_avg", 1),
                           ("ranked fighter count", "ranked_fighters", 1)):
        print(f"    {lab:<24}{spearman(ours, [sign * r[key] for r in have]):>7.3f}")
    miss = [r["name"] for r in fm if r["promotion_id"] not in rows]
    if miss: print(f"    not scored by us: {', '.join(miss)}")
    print(f"\n  {'promotion':<28}{'ours':>7}{'ourRk':>7}   FM ranks: avg / pts / ptavg / count")
    fmr = {k: {r["promotion_id"]: i + 1 for i, r in
               enumerate(sorted(fm, key=lambda r: (r[k] if k != "avg_rank" else -r[k]),
                                reverse=True))}
           for k in ("avg_rank", "point_total", "point_avg", "ranked_fighters")}
    for r in sorted(have, key=lambda r: -rows[r["promotion_id"]]["norm"]):
        p = r["promotion_id"]
        print(f"  {r['name'][:27]:<28}{rows[p]['norm']:>7.3f}{rank.get(p, 0) or '-':>7}   "
              + " / ".join(f"{fmr[k][p]:>2}" for k in
                          ("avg_rank", "point_total", "point_avg", "ranked_fighters")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
