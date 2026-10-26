#!/usr/bin/env python3
"""Stage the parsed Tapology tables as Parquet for the lake's ``parsed/`` zone.

Each JSONL table becomes
``parsed/entity=<table>/fetch_date=YYYY-MM-DD/data_0.parquet``, unfiltered -- scope
filters belong in REFINED, not LANDING (DESIGN.md §5).

Page-level rows are partitioned by the fetch date of the archived page they were parsed
from, and carry ``_raw_html_key``: the exact object key of that page in ``raw-html/``.
The key comes from ``stage_archive_for_s3.plan()``, the same function that laid out the
uploaded archive, so the two cannot drift. ``events`` and the two promotion tables were
parsed from index pages that were never archived; they get the JSONL's mtime as their
fetch date and a null key.

Needs DuckDB (``.venv/bin/pip install duckdb``).

    .venv/bin/python scripts/stage_parsed_for_gcs.py                  # dry run
    .venv/bin/python scripts/stage_parsed_for_gcs.py --commit
    gcloud storage rsync -r _parsedstage/ gs://<bucket>/
"""
import argparse, datetime, shutil, sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage_archive_for_s3 import STAGE as ARCHIVE_STAGE, plan as archive_plan

ROOT  = Path(__file__).resolve().parent.parent
DATA  = ROOT / "data"
STAGE = ROOT / "_parsedstage"

# entity name -> (source file, (archive entity, slug column) or None)
TABLES = {
    "events":            ("events.jsonl",            None),
    "event_details":     ("event_details.jsonl",     ("event", "event_slug")),
    "bouts":             ("bouts.jsonl",             ("event", "event_slug")),
    "event_fighters":    ("event_fighters.jsonl",    ("event", "event_slug")),
    "bout_details":      ("bout_details.jsonl",      ("bout",  "bout_slug")),
    "promotions":        ("promotions.jsonl",        None),
    "promotion_details": ("promotions_detail.jsonl", None),
}


def load_pages(con):
    """Every archived page as (entity, slug, fetch_date, raw_html_key)."""
    rows, _, dupes = archive_plan()
    if dupes:
        sys.exit(f"archive has {len(dupes)} key collisions -- run stage_archive_for_s3.py")
    pages = []
    for _, dst in rows:
        key = dst.relative_to(ARCHIVE_STAGE).as_posix()
        parts = dict(p.split("=", 1) for p in key.split("/")[1:-1])
        pages.append((parts["entity"], Path(key).name[: -len(".html.gz")], parts["fetch_date"], key))
    con.execute("CREATE TABLE pages (entity VARCHAR, slug VARCHAR, fetch_date DATE, raw_html_key VARCHAR)")
    con.executemany("INSERT INTO pages VALUES (?, ?, ?, ?)", pages)
    n, distinct = con.execute("SELECT count(*), count(DISTINCT (entity, slug)) FROM pages").fetchone()
    if n != distinct:
        sys.exit(f"REFUSING: {n - distinct} slugs archived more than once")
    return n


def source_sql(src, link):
    # sample_size=-1 infers the schema from every row, so sparse keys (production.*,
    # bout_counts.*) are not dropped.
    read = f"read_json('{src}', format='newline_delimited', sample_size=-1)"
    if link is None:
        mtime = datetime.date.fromtimestamp(src.stat().st_mtime).isoformat()
        return (f"SELECT t.*, CAST(NULL AS VARCHAR) AS _raw_html_key, DATE '{mtime}' AS fetch_date "
                f"FROM {read} t")
    entity, col = link
    return (f"SELECT t.*, p.raw_html_key AS _raw_html_key, p.fetch_date "
            f"FROM {read} t LEFT JOIN pages p ON p.entity = '{entity}' AND p.slug = t.{col}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true", help="write the Parquet staging tree")
    ap.add_argument("--only", nargs="+", choices=TABLES, help="limit to these tables")
    args = ap.parse_args()

    con = duckdb.connect()
    print(f"archived pages : {load_pages(con):,}\n")

    print(f"  {'table':<18} {'rows':>10} {'unlinked':>9} {'partitions':>10}")
    plans = []
    for name in args.only or TABLES:
        file, link = TABLES[name]
        src = DATA / file
        lines = sum(1 for _ in open(src, "rb"))
        con.execute(f"CREATE TEMP VIEW src_{name} AS {source_sql(src, link)}")
        rows, unlinked, parts = con.execute(
            f"SELECT count(*), count(*) FILTER (WHERE fetch_date IS NULL), "
            f"count(DISTINCT fetch_date) FROM src_{name}").fetchone()
        print(f"  {name:<18} {rows:>10,} {unlinked:>9,} {parts:>10}")
        if rows != lines:
            sys.exit(f"REFUSING: {name} read {rows:,} rows from {lines:,} lines -- a join fanned out")
        if unlinked:
            sys.exit(f"REFUSING: {unlinked:,} {name} rows have no archived page")
        plans.append((name, rows))

    if not args.commit:
        print(f"\ndry run. --commit to write {STAGE}/parsed/")
        return

    print()
    for name, rows in plans:
        out = STAGE / "parsed" / f"entity={name}"
        if out.exists():
            shutil.rmtree(out)  # a table is rebuilt whole, never merged into
        out.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY src_{name} TO '{out}' (FORMAT parquet, COMPRESSION zstd, "
                    f"PARTITION_BY (fetch_date), WRITE_PARTITION_COLUMNS true)")
        written = con.execute(f"SELECT count(*) FROM read_parquet('{out}/*/*.parquet')").fetchone()[0]
        size = sum(f.stat().st_size for f in out.rglob("*.parquet"))
        if written != rows:
            sys.exit(f"REFUSING: {name} wrote {written:,} of {rows:,} rows")
        print(f"  {name:<18} {written:>10,} rows  {size / 2**20:>8.1f} MiB  -> {out.relative_to(ROOT)}")

    print(f"\nnext:\n  gcloud storage rsync -r {STAGE.relative_to(ROOT)}/ gs://<bucket>/")


if __name__ == "__main__":
    main()
