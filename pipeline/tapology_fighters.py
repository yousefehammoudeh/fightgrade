"""Tapology fighter pages -> every bout on a fighter's record -> the lake -> LANDING.

    .venv/bin/python -m pipeline.tapology_fighters --plan                 # write the worklist to the lake
    .venv/bin/python -m pipeline.tapology_fighters --slugs a,b --dry-run  # fetch and stage locally only
    .venv/bin/python -m pipeline.tapology_fighters --shard 0 --shards 3   # one shard of the plan

Why: the event crawl walks event pages to their bouts, and Tapology carries many fights
(mostly regional, mostly before 2015) only on the fighters' records, some with no event
page and some with no bout page either. The bout sitemap does not list them all
(experiments/elo_mma/README.md). One fighter page holds the whole record, every sport,
with the bout id, both records before the fight, the method, date and promotion.

The plan is the fighters whose Tapology record is longer than what we hold, from the
census in data/elo/record_gap.parquet (each fighter's last `record_after` against the
bouts held), most-incomplete first. It is fixed once written, so shards split it the
way the event backfill's do: every shards-th fighter, minus what is archived by now,
so a shard cut short is simply run again.

The method is the event crawl's (pipeline.tapology): its Browser, polite_fetch, per-IP
lock and daily ledger, 4 s spacing. Rows land in one table, fighter_records: one row
per (fighter page, bout row), so a bout appears once from each fighter we fetched.
"""
import argparse, gzip, json, shutil, sys
from collections import Counter
from datetime import date
from pathlib import Path

from . import tapology as tap
from .lake import Lake, environment, new_run_id, raw_html_key, zone

sys.path.insert(0, str(tap.ROOT / "scrapers" / "extract"))
from tapology_extract import parse_fighter_page  # noqa: E402

TABLE = "fighter_records"
ENTITY = "fighter"
GAP_CENSUS = tap.ROOT / "data" / "elo" / "record_gap.parquet"
MIN_RECORD = 5              # pro MMA fights on the Tapology record; the Elo ranking threshold
DAILY_BUDGET = 1500         # per IP per day; the 2026-10-26 event backfill ran clean at this
FIGHTER_MARK = "fighter-record-bout"
DBT_SELECT = "lnd_tapology_fighter_records"
# Fighters in a row that gave up after polite_fetch's retries. Tapology's "Temporarily
# Unavailable" page arrives as a normal response, not a 503, so it never trips the rate
# limit stop: on 2026-10-28 all three prod shards spent hours on it, three requests and
# ~70 s per fighter, while what they had fetched sat unloaded. Ten is ~12 minutes of it.
MAX_MISS_STREAK = 10


def plan_key(env: str) -> str:
    return f"{zone(env)}state/tapology/fighter_records/plan.json"


def record_rows(f: dict, key: str, fetch_date: str) -> list[dict]:
    """One fighter page -> one row per bout on its record."""
    page = {k: f[k] for k in ("fighter_slug", "fighter_name", "fighter_url", "page_title")}
    rows = []
    for b in f["bouts"]:
        rows.append({**page, **b, "_raw_html_key": key, "fetch_date": fetch_date})
    return rows


def qa(f: dict) -> list[dict]:
    """Anomalies worth a look. A page with no record rows is fatal; the rest are
    properties of real records (an opponent with no profile, a year-only date)."""
    out = []
    slug = f.get("fighter_slug") or "?"
    if not f["bouts"]:
        out.append({"slug": slug, "code": "parse_failed", "detail": "no record rows"})
    for b in f["bouts"]:
        if b["bout_id"] is None:
            out.append({"slug": slug, "code": "bout_without_id", "detail": str(b.get("opponent_name"))})
        if b["status"] not in ("cancelled", "upcoming") and b["date_precision"] != "full":
            out.append({"slug": slug, "code": "date_not_full", "detail": f"{b['bout_id']} {b['year']} {b['month_day']}"})
        if b["status"] not in ("cancelled", "upcoming") and not b["opponent_slug"]:
            out.append({"slug": slug, "code": "opponent_without_slug", "detail": f"{b['bout_id']} {b['opponent_name']}"})
    return out


def write_plan(lake: Lake, env: str, min_record: int = MIN_RECORD) -> dict:
    """The worklist from the local census: no requests to Tapology."""
    import pandas as pd

    g = pd.read_parquet(GAP_CENSUS)
    g = g[(g["theirs"] >= min_record) & (g["gap"] > 0)].sort_values(["gap", "theirs"], ascending=False)
    plan = {"created": date.today().isoformat(), "min_record": min_record, "census": str(GAP_CENSUS.name),
            "bouts_missing": int(g["gap"].sum()), "slugs": g["slug"].tolist()}
    lake.write_json(plan_key(env), plan)
    tap.log(f"fighter plan: {len(plan['slugs']):,} fighters, {plan['bouts_missing']:,} bouts missing from what "
            f"we hold, in {plan_key(env)}")
    return plan


def shard_slugs(lake: Lake, env: str, shard: int, shards: int) -> list[str]:
    plan = lake.read_json(plan_key(env), None)
    if plan is None:
        raise RuntimeError("no fighter plan: run with --plan first")
    return [s for i, s in enumerate(plan["slugs"]) if i % shards == shard]


def archived(lake: Lake, env: str) -> set[str]:
    zones = ("",) if env == "prod" else ("", "dev/")
    return lake.archived_slugs("tapology", ENTITY, zones)


def crawl(slugs: list[str], limit: int, sleep: float, budget: tap.Budget, env: str, run_dir: Path) -> dict:
    """Fetch, archive locally and parse each fighter page, until the list or the budget runs out."""
    from playwright.sync_api import sync_playwright

    fetch_date = date.today().isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps({"fetch_date": fetch_date, "env": env, "entity": ENTITY}))
    rows, pages, anomalies, report, stopped = {TABLE: []}, [], [], Counter(), None
    streak = 0
    with sync_playwright() as pw:
        br = tap.Browser(pw, budget)
        try:
            if not br.open():
                raise tap.Blocked("Cloudflare did not clear on the first page")
            tap.log(f"browser ready, Cloudflare cleared; {len(slugs):,} fighters to fetch")
            for slug in slugs:
                if limit and report["fighters_kept"] >= limit:
                    break
                url = f"{tap.BASE}/fightcenter/fighters/{slug}"
                html, status = tap.polite_fetch(br, url, tap.c3.JS, sleep)
                if html is None:
                    report[f"fighter_miss_{status}"] += 1
                    streak = streak + 1 if status.startswith("gave_up_") else 0
                    if streak >= MAX_MISS_STREAK:
                        raise tap.Stop(f"{streak} fighters in a row gave up (last {status} on {slug})")
                    continue
                streak = 0
                if FIGHTER_MARK not in html:
                    report["fighter_bad_page"] += 1
                    continue
                key = raw_html_key("tapology", ENTITY, fetch_date, slug, env)
                local = run_dir / "raw" / ENTITY / f"{slug}.html.gz"
                local.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(local, "wb", compresslevel=6) as f:     # archive BEFORE loading
                    f.write(html.encode("utf-8"))
                pages.append((local, key))
                try:
                    f = parse_fighter_page(html, url)
                except Exception as ex:                    # a parser bug must not stop the crawl
                    anomalies.append({"slug": slug, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
                    report["fighter_parse_error"] += 1
                    continue
                anomalies += qa(f)
                rows[TABLE] += record_rows(f, key, fetch_date)
                report["fighters_kept"] += 1
                if report["fighters_kept"] % 25 == 0:
                    tap.log(f"  {report['fighters_kept']} fighters, {len(rows[TABLE]):,} record rows")
        except tap.Stop as e:
            stopped = f"stopped: {e}"
            tap.log(stopped)
        finally:
            br.close()
    report.update(requests=budget.spent, record_rows=len(rows[TABLE]))
    return {"fetch_date": fetch_date, "rows": rows, "pages": pages, "report": dict(report),
            "anomalies": anomalies, "stopped": stopped}


def rebuild(run_dir: Path, env: str) -> dict:
    """Re-parse a run's archived fighter pages with no network."""
    fetch_date = json.loads((run_dir / "meta.json").read_text())["fetch_date"]
    rows, pages, anomalies, report = {TABLE: []}, [], [], Counter()
    for local in sorted((run_dir / "raw" / ENTITY).glob("*.html.gz")):
        slug = local.name[: -len(".html.gz")]
        url = f"{tap.BASE}/fightcenter/fighters/{slug}"
        f = parse_fighter_page(gzip.decompress(local.read_bytes()).decode("utf-8"), url)
        key = raw_html_key("tapology", ENTITY, fetch_date, slug, env)
        pages.append((local, key))
        anomalies += qa(f)
        rows[TABLE] += record_rows(f, key, fetch_date)
        report["fighters_kept"] += 1
    return {"fetch_date": fetch_date, "rows": rows, "pages": pages, "report": dict(report),
            "anomalies": anomalies, "stopped": None}


def verify(result: dict, staged: dict, run_dir: Path, env: str, lake: Lake | None) -> dict:
    """The event crawl's tests 4-6, for this table: archive, parse quality, Parquet."""
    fails, warns = [], []
    recs, raw_keys = result["rows"][TABLE], {k for _, k in result["pages"]}
    for local, key in result["pages"]:
        try:
            gzip.decompress(local.read_bytes())
        except OSError:
            fails.append(f"corrupt gzip: {local.name}")
        if lake and lake.size(key) != local.stat().st_size:
            fails.append(f"raw page missing or wrong size in bucket: {key}")
    codes = Counter(a["code"] for a in result["anomalies"])
    for a in result["anomalies"]:
        if a["code"] in tap.FATAL_QA:
            fails.append(f"qa {a['code']}: {a['slug'][:50]} {a['detail'][:60]}")
    warns += [f"qa {c}: {n} rows" for c, n in codes.items() if c not in tap.FATAL_QA]
    n = staged.get(TABLE, (0, []))[0]
    if n != len(recs):
        fails.append(f"{TABLE}: {len(recs)} rows parsed but {n} in Parquet")
    if {r["_raw_html_key"] for r in recs} - raw_keys:
        fails.append(f"{TABLE}: rows point at an unarchived page")
    if lake:
        for _, files in staged.values():
            for f in files:
                if lake.size(tap.parsed_key(f, run_dir, env)) != f.stat().st_size:
                    fails.append(f"Parquet missing or wrong size in bucket: {tap.parsed_key(f, run_dir, env)}")
    fails += tap.roundtrip_mismatches(result["rows"], staged)
    if result["stopped"]:
        warns.append(result["stopped"])
    return {"fails": fails, "warns": warns}


def load_landing_cli(env: str) -> bool:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(["build", "--select", DBT_SELECT, "--target", env,
                              "--project-dir", str(tap.ROOT / "dbt"), "--profiles-dir", str(Path("~/.dbt").expanduser())])
    return bool(res.success)


def landing_count(run_id: str, staged: dict, env: str) -> dict:
    cur = tap.snowflake_cursor(env)
    cur.execute(f"select count(*) from FIGHTGRADE_{env.upper()}.LANDING.LND_TAPOLOGY_FIGHTER_RECORDS "
                f"where _object_key like %s", (f"%part-{run_id}-%",))
    return {"parquet": staged.get(TABLE, (0, []))[0], "landing": cur.fetchone()[0]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true", help="write the worklist to the lake from the local census")
    ap.add_argument("--min-record", type=int, default=MIN_RECORD, help="with --plan: fighters with this many fights")
    ap.add_argument("--slugs", help="comma-separated fighter slugs to fetch instead of a shard")
    ap.add_argument("--shard", type=int, default=-1)
    ap.add_argument("--shards", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="fighter pages to keep (0: as many as the budget allows)")
    ap.add_argument("--sleep", type=float, default=tap.DEFAULT_SLEEP)
    ap.add_argument("--max-requests", type=int, default=DAILY_BUDGET)
    ap.add_argument("--max-minutes", type=float, default=600)
    ap.add_argument("--dry-run", action="store_true", help="fetch and stage locally; upload and load nothing")
    ap.add_argument("--from-run", metavar="RUN_ID", help="re-parse that run's archived pages (no network)")
    args = ap.parse_args()
    if args.sleep < 2.0:
        ap.error("--sleep below 2.0 draws Cloudflare challenges from one IP (see pipeline.tapology.DEFAULT_SLEEP)")

    env = environment()
    lake = Lake(env)
    if args.plan:
        write_plan(lake, env, args.min_record)
        return 0
    run_id = args.from_run or new_run_id()
    run_dir = tap.RUNS / run_id
    tap.log(f"run {run_id} env={env} zone='{zone(env)}' -> {run_dir.relative_to(tap.ROOT)}")
    if args.from_run:
        result = rebuild(run_dir, env)
    else:
        if args.slugs:
            todo = [s.strip() for s in args.slugs.split(",") if s.strip()]
        elif args.shards:
            mine = shard_slugs(lake, env, args.shard, args.shards)
            done = archived(lake, env)
            todo = [s for s in mine if s not in done]
            tap.log(f"shard {args.shard}/{args.shards}: {len(mine):,} fighters, {len(todo):,} not archived yet")
        else:
            ap.error("give --plan, --slugs, --shard/--shards or --from-run")
        with tap.ip_budget(lake, args.max_requests, args.max_minutes, DAILY_BUDGET) as budget:
            result = crawl(todo, args.limit, args.sleep, budget, env, run_dir)
    shutil.rmtree(run_dir / "tables", ignore_errors=True)
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = tap.stage(result["rows"], run_dir, run_id)
    if not args.dry_run:
        n_pages, n_files = tap.upload(lake, result, staged, run_dir, env)
        tap.log(f"uploaded {n_pages} pages and {n_files} Parquet files")
    checks = verify(result, staged, run_dir, env, None if args.dry_run else lake)
    report = {"run_id": run_id, "env": env, "fetch_date": result["fetch_date"], "crawl": result["report"],
              "rows": {t: len(r) for t, r in result["rows"].items()}, **checks}
    if not args.dry_run and not checks["fails"] and staged:
        report["dbt_success"] = load_landing_cli(env)
        report["landing"] = landing_count(run_id, staged, env)
        if report["landing"]["parquet"] != report["landing"]["landing"]:
            report["fails"].append(f"LANDING {TABLE}: {report['landing']} rows")
        if not report["dbt_success"]:
            report["fails"].append(f"dbt build --select {DBT_SELECT} failed")
    (run_dir / "report.json").write_text(json.dumps(report, indent=1))
    tap.log("report:\n" + json.dumps(report, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
