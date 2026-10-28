"""Which of Tapology's bouts the corpus does not hold.

    python3 experiments/bout_sitemap/diff.py   # after fetch.py -> data/sitemaps/bouts_missing.parquet

Every bout id in the bout sitemaps against data/bouts.jsonl. The sitemap carries no sport or
fighters, so the missing set is all sports; the slug alone names the two fighters, which is
enough to find the bouts of chosen fighters (Silva's twelve) and to rank fighters by how many
of their bouts are missing.
"""
import gzip
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SITEMAPS = ROOT / "data" / "sitemaps" / "bouts"
OUT = ROOT / "data" / "sitemaps" / "bouts_missing.parquet"
ENTRY = re.compile(r"<loc>[^<]*/fightcenter/bouts/(\d+)-([^<]*)</loc>(?:\s*<lastmod>([^<]*)</lastmod>)?")


def main():
    rows = []
    for path in sorted(SITEMAPS.glob("*.gz")):
        with gzip.open(path, "rt") as f:
            rows += [(int(i), s, m, path.name) for i, s, m in ENTRY.findall(f.read())]
    site = pd.DataFrame(rows, columns=["bout_id", "bout_slug", "lastmod", "sitemap"]).drop_duplicates("bout_id")
    held = set()
    with open(ROOT / "data" / "bouts.jsonl") as f:
        for line in f:
            held.add(json.loads(line)["bout_id"])
    site["held"] = site["bout_id"].isin(held)
    missing = site[~site["held"]]
    print(f"{site.sitemap.nunique()} sub-sitemaps, {len(site):,} bouts listed, {site.held.sum():,} held, {len(missing):,} missing, "
          f"{len(held - set(site.bout_id)):,} held but not listed")
    missing.to_parquet(OUT, index=False)
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
