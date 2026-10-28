#!/usr/bin/env python3
"""Derive the canonical modelling population and each fighter's inferred gender.

No network. Reads data/bouts.jsonl + data/event_details.jsonl, writes
data/fighter_gender.jsonl (one row per fighter in the corpus, flagged in/out).

Decisions this encodes are argued in docs/fighter-graph-and-gender.md. In short:

  population = giant component of the opponent graph, minus kids-only, juniors-only,
               closed pairs, isolated fighters, and fighters whose gender cannot be
               inferred without a manual ground truth.

  gender     = two-sided majority label propagation seeded from title strings, with
               modified_rules edges excluded (4x more likely to be intergender) and
               male seeding refused on combined-sex events (whose per-bout strings
               drop the "and Women's" half).

  python3 scrapers/derive/fighter_population.py [--out PATH]
"""
import argparse, json, collections, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
FEM = re.compile(r"\b(Women|Women's|Womens|Woman|Female|Feamale|Ladies)\b", re.I)
MAL = re.compile(r"\b(Men's|Mens|Male)\b")


def load():
    evname = {}
    for line in (DATA / "event_details.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        evname[d["event_slug"]] = d.get("event_name") or ""
    nodes, all_e, prop_e, mod_e = set(), [], [], []
    lvl = collections.defaultdict(collections.Counter)
    nb = collections.Counter()
    seedvote = collections.defaultdict(set)
    for line in (DATA / "bouts.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            b = json.loads(line)
        except Exception:
            continue
        fs = [f.get("fighter_slug") for f in (b.get("fighters") or []) if f.get("fighter_slug")]
        for s in fs:
            nodes.add(s); lvl[s][b.get("level")] += 1; nb[s] += 1
        if len(fs) == 2 and fs[0] != fs[1]:
            e = (fs[0], fs[1]); all_e.append(e)
            (mod_e if b.get("modified_rules") else prop_e).append(e)
        tb = b.get("title_bout") or ""
        ev = evname.get(b["event_slug"], "")
        # A combined-sex event's per-bout string cannot be trusted for MALE seeding:
        # Tapology drops the "and Women's" half, labelling women's bouts "Men's".
        combined = bool(FEM.search(ev) and MAL.search(ev))
        if FEM.search(tb):
            for s in fs: seedvote[s].add("F")
        elif MAL.search(tb) and not combined:
            for s in fs: seedvote[s].add("M")
    return nodes, all_e, prop_e, lvl, nb, seedvote


def components(edges, universe):
    par = {}
    def find(x):
        par.setdefault(x, x); r = x
        while par[r] != r: r = par[r]
        while par[x] != r: par[x], x = r, par[x]
        return r
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb: par[ra] = rb
    return {n: find(n) for n in universe}


def propagate(prop_e, fseed, mseed):
    adj = collections.defaultdict(list)
    for a, b in prop_e:
        adj[a].append(b); adj[b].append(a)
    F, M = set(fseed), set(mseed)
    while True:
        addF, addM = set(), set()
        for f, ops in adj.items():
            if f in F or f in M:
                continue
            kf = sum(1 for o in ops if o in F)
            km = sum(1 for o in ops if o in M)
            if kf + km == 0:
                continue
            # majority of the opponents whose gender is KNOWN; unlabelled opponents do
            # not count in the denominator (scoring against all opponents collapses
            # recall to 79%, since early on almost nobody is labelled)
            (addF if kf / (kf + km) >= 0.5 else addM).add(f)
        if not addF and not addM:
            break
        F |= addF; M |= addM
    return F, M


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DATA / "fighter_gender.jsonl"))
    args = ap.parse_args()

    nodes, all_e, prop_e, lvl, nb, seedvote = load()
    fseed = {s for s, v in seedvote.items() if v == {"F"}}
    mseed = {s for s, v in seedvote.items() if v == {"M"}}
    dropped_conflict = sum(1 for v in seedvote.values() if len(v) > 1)

    comp = components(all_e, nodes)
    csize = collections.Counter(comp.values())
    giant = csize.most_common(1)[0][0]
    F, M = propagate(prop_e, fseed, mseed)

    def where(s):
        n = csize[comp[s]]
        return "giant" if comp[s] == giant else ("isolated" if n == 1 else
               ("closed_pair" if n == 2 else "island"))

    rows = 0
    counts = collections.Counter()
    with open(args.out, "w", encoding="utf-8") as fh:
        for s in sorted(nodes):
            levels = sorted(k for k in lvl[s] if k)
            g = "F" if s in F else ("M" if s in M else None)
            src = ("seed" if s in fseed or s in mseed else ("propagated" if g else None))
            loc = where(s)
            youth_only = bool(levels) and set(levels) <= {"Kids", "Juniors"}
            in_pop = (loc == "giant" and not youth_only and g is not None)
            counts[loc] += 1
            if in_pop: counts["population"] += 1
            if in_pop and g: counts["pop_" + g] += 1
            fh.write(json.dumps({"fighter_slug": s, "gender": g, "gender_source": src,
                                 "bouts": nb[s], "levels": levels, "component": loc,
                                 "in_population": in_pop}, ensure_ascii=False) + "\n")
            rows += 1
    print(f"wrote {rows:,} rows -> {args.out}")
    print(f"  seeds: {len(fseed):,} female, {len(mseed):,} male "
          f"({dropped_conflict} contradictory dropped)")
    print(f"  graph: giant {counts['giant']:,} | island {counts['island']:,} | "
          f"closed_pair {counts['closed_pair']:,} | isolated {counts['isolated']:,}")
    print(f"  gender labelled corpus-wide: {len(F):,} F + {len(M):,} M = {len(F)+len(M):,}")
    print(f"\n  CANONICAL MODELLING POPULATION: {counts['population']:,}")
    print(f"    female {counts['pop_F']:,}   male {counts['pop_M']:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
