#!/usr/bin/env python3
"""Stage the raw HTML archive for S3 under a partitioned key layout.

The archive on disk is sha1-sharded (``data/html/<aa>/<bb>/<slug>.html.gz``), which gives
no way to replay one fetch date. S3 wants
``raw-html/source=tapology/entity=<event|bout>/fetch_date=YYYY-MM-DD/<slug>.html.gz``.

Rather than upload file-by-file, this builds a staging tree of **hardlinks** mirroring the
target layout, costing no disk, and leaves the upload to ``aws s3 sync`` -- which is
parallel, resumable and idempotent for free.

    python3 scripts/stage_archive_for_s3.py                 # dry run
    python3 scripts/stage_archive_for_s3.py --commit
    aws s3 sync _s3stage/ s3://<bucket>/ --no-progress

fetch_date comes from file mtime, which matches the documented crawl windows
(the 2026-08-26 bucket is the 8,101 cancelled-title-bout crawl, 2026-08-27 the
11,287-page gap crawl).
"""
import argparse, collections, datetime, os, sys
from pathlib import Path

ROOT  = Path(__file__).resolve().parent.parent
STAGE = ROOT / "_s3stage"
SOURCES = [(ROOT / "data" / "html", "event"), (ROOT / "data" / "html_bouts", "bout")]


def plan():
    rows, per_part, collisions = [], collections.Counter(), collections.defaultdict(list)
    for base, entity in SOURCES:
        if not base.is_dir():
            sys.exit(f"missing archive directory: {base}")
        for src in base.rglob("*.html.gz"):
            d = datetime.date.fromtimestamp(src.stat().st_mtime).isoformat()
            key = f"raw-html/source=tapology/entity={entity}/fetch_date={d}/{src.name}"
            rows.append((src, STAGE / key))
            per_part[(entity, d)] += 1
            collisions[key].append(src)
    dupes = {k: v for k, v in collisions.items() if len(v) > 1}
    return rows, per_part, dupes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="build the staging tree")
    args = ap.parse_args()

    rows, per_part, dupes = plan()
    total_bytes = sum(s.stat().st_size for s, _ in rows)

    print(f"archive files : {len(rows):,}")
    print(f"archive size  : {total_bytes / 2**30:.2f} GiB")
    print(f"partitions    : {len(per_part)}")
    print(f"\n  {'entity':<7} {'fetch_date':<12} {'files':>8}")
    for (entity, d), n in sorted(per_part.items()):
        print(f"  {entity:<7} {d:<12} {n:>8,}")

    puts = len(rows)
    print(f"\nestimated S3 cost")
    print(f"  PUT requests  : {puts:,} x $0.005/1k = ${puts / 1000 * 0.005:.2f}  (one-off)")
    print(f"  storage       : {total_bytes / 2**30:.2f} GiB x $0.023/GiB/mo = "
          f"${total_bytes / 2**30 * 0.023:.2f}/month")
    print(f"  transfer IN   : $0.00  (ingress is free)")

    if dupes:
        print(f"\nREFUSING: {len(dupes)} key collisions, e.g.")
        for k, v in list(dupes.items())[:3]:
            print(f"  {k}\n    " + "\n    ".join(str(p) for p in v))
        sys.exit(1)

    if not args.commit:
        print(f"\ndry run. --commit to build {STAGE} (hardlinks, no extra disk).")
        return

    made = skipped = 0
    for src, dst in rows:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            skipped += 1
            continue
        try:
            os.link(src, dst)
            made += 1
        except OSError as e:
            sys.exit(f"hardlink failed ({e}) -- is {STAGE} on the same filesystem as data/?")
    print(f"\nstaged  : {made:,} hardlinked, {skipped:,} already present")
    print(f"tree    : {STAGE}")
    print(f"\nnext:\n  aws s3 sync {STAGE}/ s3://<bucket>/ --no-progress")


if __name__ == "__main__":
    main()
