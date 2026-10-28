"""Tapology's bout sitemaps, for the bout-level completeness diff.

    python3 experiments/bout_sitemap/fetch.py     # -> data/sitemaps/bouts/*.xml.gz, ~17 min

Headed Chrome on this machine through pipeline.tapology's Browser and polite_fetch, at the
crawler's 4 s spacing: the sitemap index, then every bout sub-sitemap it lists (252 last time).
Resumable: a sub-sitemap already saved is skipped. No event or bout pages are fetched.
"""
import gzip
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pipeline.tapology as tap  # noqa: E402

OUT = ROOT / "data" / "sitemaps" / "bouts"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    budget = tap.Budget(limit=400, minutes=60, why="sitemap budget spent")
    with sync_playwright() as pw:
        br = tap.Browser(pw, budget)
        try:
            if not br.open():
                raise SystemExit("Cloudflare did not clear on the first page")
            index, status = tap.polite_fetch(br, tap.SITEMAP_INDEX, tap.SITEMAP_JS, tap.DEFAULT_SLEEP)
            if not index:
                raise SystemExit(f"sitemap index: {status}")
            (OUT.parent / "index.xml").write_text(index)
            subs = re.findall(r"<loc>([^<]*/bouts/sitemap(?:_\d+)?\.xml)</loc>", index)
            tap.log(f"{len(subs)} bout sub-sitemaps")
            for i, url in enumerate(subs, 1):
                path = OUT / (url.rsplit("/", 1)[-1] + ".gz")
                if path.exists():
                    continue
                xml, status = tap.polite_fetch(br, url, tap.SITEMAP_JS, tap.DEFAULT_SLEEP)
                if xml is None:
                    tap.log(f"  {path.name}: {status}")
                    continue
                with gzip.open(path, "wt") as f:
                    f.write(xml)
                if i % 25 == 0:
                    tap.log(f"  {i}/{len(subs)}")
            tap.log(f"done: {len(list(OUT.glob('*.gz')))}/{len(subs)} saved, {budget.spent} requests")
        finally:
            br.close()


if __name__ == "__main__":
    main()
