"""MMA news articles: the pages behind flagged headlines -> raw HTML in the lake -> LANDING.

    .venv/bin/python -m pipeline.news_articles --gold                # the gold set's candidates
    .venv/bin/python -m pipeline.news_articles --flagged             # every headline the classifier flags
    .venv/bin/python -m pipeline.news_articles --ids FILE --dry-run
    .venv/bin/python -m pipeline.news_articles --replay              # re-parse the archive, no requests

A headline cannot always say whose retirement it is, or whether it is a fact or
talk ("Stipe Miocic issues first statement Jon Jones retirement"); the article
does. The owners of MMA Fighting and MMA Mania agreed to article pages where
needed (2026-10-31), so only those pages are read: the headlines the classifier
flags as a retirement, return, withdrawal or replacement, at any status, plus
the gold set's candidates. The sitemap crawl's rules hold: plain HTTP, the same
honest user agent, at least MIN_INTERVAL between requests on a site, the site's
one lock (so this never runs beside news_crawl on the same site), and every page
archived as served before anything is parsed from it.

Both sites carry a schema.org NewsArticle in JSON-LD: the real headline, the
description, datePublished (the exact time, which step 6 needs for days'
notice) and articleBody. One row per page goes to LANDING lnd_news_articles.
The two sites are read at once, one thread each, so a backfill of ~6k pages
takes ~5 hours.

--replay re-parses the latest archived copy of every page in this environment's
zone, straight from the bucket, and loads it again: a parser change is replayed,
and PROD is filled from a copy of DEV's archive without asking the sites twice.
"""
import argparse, gzip, html, json, re, shutil, sys, threading, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from .lake import Lake, environment, new_run_id, raw_html_key, zone
from .news_sitemaps import MIN_INTERVAL, SITES, Fetcher, site_lock
from .tapology import Budget, Stop, parsed_key, roundtrip_mismatches, snowflake_cursor, stage

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "_crawlruns" / "news_articles"
TABLE = "news_articles"
DBT_SELECT = "lnd_news_articles"     # its own table only: the slug runs load lnd_news_events beside it
FLAG_TYPES = ("retirement", "return", "withdrawal", "replacement")
BACKFILL_BUDGET = 5000      # per site
MAX_FETCH_MINUTES = 8 * 60
LD_JSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def page_name(news_url_id: str) -> str:
    """'mmafighting:vox:21532169' -> 'mmafighting_vox_21532169', the archived page's name."""
    return news_url_id.replace(":", "_")


def page_id(name: str) -> str:
    """The reverse of page_name. Every archived name is site_era_digits (sites and eras have no '_')."""
    site, era, article_id = name.split("_", 2)
    return f"{site}:{era}:{article_id}"


def clean(text: str | None) -> str | None:
    if text is None:
        return None
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def parse_article(page: str) -> dict:
    """The page's NewsArticle: headline, description, published and modified times, author,
    section and body. Raises if the page has none."""
    for blob in LD_JSON.findall(page):
        try:
            d = json.loads(blob)
        except json.JSONDecodeError:
            continue
        for x in (d if isinstance(d, list) else d.get("@graph", [d])):
            if isinstance(x, dict) and "articleBody" in x:
                author = x.get("author")
                author = author[0] if isinstance(author, list) and author else author
                page_url = x.get("url") or x.get("mainEntityOfPage")
                return {"page_url": page_url if isinstance(page_url, str) else None,
                        "headline": clean(x.get("headline")), "description": clean(x.get("description")),
                        "date_published": x.get("datePublished"), "date_modified": x.get("dateModified"),
                        "author": clean(author.get("name")) if isinstance(author, dict) else None,
                        "section": clean(x.get("articleSection") if isinstance(x.get("articleSection"), str) else None),
                        "body": clean(x["articleBody"])}
    raise ValueError("no NewsArticle with an articleBody in the page's JSON-LD")


def archived(lake: Lake, env: str) -> set[str]:
    zones = ("",) if env == "prod" else ("", "dev/")
    return lake.archived_slugs("news", "article", zones)


def crawl_site(site: str, todo: list[dict], interval: float, env: str, run_dir: Path, fetch_date: str,
               lake: Lake | None, budget: Budget, out: dict) -> None:
    """Fetch and archive one site's pages, under the site's lock. Fills out[site]."""
    pages, anomalies, stopped, report = [], [], None, Counter()
    lock = site_lock(lake, site) if lake else None
    held = lock.__enter__() if lock else True
    try:
        if not held:
            stopped = f"{site}: another run holds the site's lock"
            return
        f = Fetcher(site, budget, interval)
        f.headers["Accept"] = "text/html"
        for i, r in enumerate(todo, 1):
            status, body = f.polite_get(r["url"])
            if body is None:
                anomalies.append({"id": r["news_url_id"], "code": "fetch_failed", "detail": str(status)})
                report["failed"] += 1
                continue
            name = page_name(r["news_url_id"])
            local = run_dir / "raw" / site / f"{name}.html.gz"
            local.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(local, "wb", compresslevel=6) as gz:
                gz.write(body)
            pages.append((local, raw_html_key("news", "article", fetch_date, name, env), r))
            report["kept"] += 1
            if i % 200 == 0:
                log(f"  {site}: {i:,} of {len(todo):,}")
    except Stop as e:
        stopped = f"{site}: stopped: {e}"
        log(stopped)
    finally:
        if lock:
            lock.__exit__(None, None, None)
        out[site] = {"pages": pages, "anomalies": anomalies, "stopped": stopped, "report": dict(report)}


def page_rows(pages: list[tuple[Path, str, dict]]) -> tuple[list[dict], list[dict]]:
    rows, anomalies = [], []
    for local, key, r in pages:
        try:
            a = parse_article(gzip.decompress(local.read_bytes()).decode("utf-8", "replace"))
        except (OSError, ValueError) as ex:
            anomalies.append({"id": r["news_url_id"], "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
            continue
        page_url = a.pop("page_url")
        rows.append({"news_url_id": r["news_url_id"], "site": r["site"], "url": r.get("url") or page_url, **a,
                     "body_chars": len(a["body"] or ""), "_raw_html_key": key,
                     "fetch_date": key.split("/fetch_date=", 1)[1].split("/", 1)[0]})
    return rows, anomalies


def run(env: str, run_id: str, lake: Lake | None, todo: list[dict], interval: float, per_site: int,
        minutes: float) -> dict:
    fetch_date, run_dir = date.today().isoformat(), RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    by_site = {s: [r for r in todo if r["site"] == s] for s in SITES}
    log("to read: " + ", ".join(f"{len(v):,} {s}" for s, v in by_site.items()))
    out: dict = {}
    threads = [threading.Thread(target=crawl_site, args=(s, rows, interval, env, run_dir, fetch_date, lake,
                                                         Budget(per_site, minutes), out))
               for s, rows in by_site.items() if rows]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    pages = [p for v in out.values() for p in v["pages"]]
    rows, parse_failures = page_rows(pages)
    report = {f"{k}_{s}": n for s, v in out.items() for k, n in v["report"].items()}
    stopped = "; ".join(v["stopped"] for v in out.values() if v["stopped"]) or None
    log(f"{len(pages):,} pages archived, {len(rows):,} parsed")
    return {"fetch_date": fetch_date, "run_dir": str(run_dir), "pages": [(p[0], p[1]) for p in pages],
            "rows": {TABLE: rows}, "anomalies": [a for v in out.values() for a in v["anomalies"]] + parse_failures,
            "report": {**report, "rows": len(rows)}, "stopped": stopped}


def replay(lake: Lake, env: str, run_id: str) -> dict:
    """Re-parse the latest archived copy of every page in this environment's zone. No requests
    to the sites; the URL comes from the page's own JSON-LD."""
    latest: dict[str, str] = {}
    prefix = f"{zone(env)}raw-html/source=news/entity=article/"
    for blob in lake.bucket.list_blobs(prefix=prefix):
        if blob.name.endswith(".html.gz"):
            name = blob.name.rsplit("/", 1)[1][: -len(".html.gz")]
            if blob.name > latest.get(name, ""):                  # fetch_date sorts as text
                latest[name] = blob.name
    run_dir = RUNS / run_id

    def fetch(item: tuple[str, str]) -> tuple[Path, str, dict]:
        name, key = item
        nid = page_id(name)
        local = run_dir / "raw" / nid.split(":", 1)[0] / f"{name}.html.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        lake.bucket.blob(key).download_to_filename(str(local))
        return local, key, {"news_url_id": nid, "site": nid.split(":", 1)[0]}

    with ThreadPoolExecutor(16) as pool:
        pages = list(pool.map(fetch, sorted(latest.items())))
    rows, parse_failures = page_rows(pages)
    log(f"replay: {len(pages):,} pages in {prefix} -> {len(rows):,} rows")
    return {"fetch_date": date.today().isoformat(), "run_dir": str(run_dir), "pages": [(p[0], p[1]) for p in pages],
            "rows": {TABLE: rows}, "anomalies": parse_failures,
            "report": {"pages_replayed": len(pages), "rows": len(rows)}, "stopped": None, "replay": True}


GOLD = """
select h.news_url_id, h.site, h.url from {DB}.LANDING.NEWS_EVENT_LABELS g
join {DB}.REFINED.STG_NEWS_HEADLINES h using (news_url_id) where h.is_candidate
"""
# The latest answer per headline from the chosen model and prompt version, flagged.
FLAGGED = """
select h.news_url_id, h.site, h.url from {DB}.REFINED.STG_NEWS_HEADLINES h
join (select payload:news_url_id::varchar as news_url_id, payload:event_type::varchar as event_type
      from {DB}.LANDING.LND_NEWS_EVENTS
      where payload:model::varchar = %s and payload:prompt_version::varchar = %s and not payload:not_mma::boolean
      qualify row_number() over (partition by news_url_id order by _loaded_at desc) = 1) e using (news_url_id)
where e.event_type in ({TYPES})
order by h.published_date desc
"""


def select(env: str, gold: bool, model: str | None = None, prompt_version: str | None = None) -> list[dict]:
    from .news_sitemaps import ensure_dev_tables
    ensure_dev_tables(env)
    cur = snowflake_cursor(env)
    db = f"FIGHTGRADE_{env.upper()}"
    if gold:
        cur.execute(GOLD.replace("{DB}", db))
    else:
        cur.execute(FLAGGED.replace("{DB}", db).replace("{TYPES}", ", ".join(f"'{t}'" for t in FLAG_TYPES)),
                    (model, prompt_version))
    return [dict(zip(("news_url_id", "site", "url"), r)) for r in cur.fetchall()]


def upload(lake: Lake, result: dict, staged: dict, run_dir: Path, env: str) -> tuple[int, int]:
    for local, key in [] if result.get("replay") else result["pages"]:
        lake.upload_file(local, key, "application/gzip")
    files = [f for _, fs in staged.values() for f in fs]
    for f in files:
        lake.upload_file(f, parsed_key(f, run_dir, env), "application/octet-stream")
    return len(result["pages"]), len(files)


def verify(result: dict, staged: dict, run_dir: Path, env: str, lake: Lake | None) -> dict:
    fails, warns = [], []
    for local, key in result["pages"]:
        if lake and lake.size(key) != local.stat().st_size:
            fails.append(f"raw page missing or wrong size in bucket: {key}")
    for a in result["anomalies"]:
        warns.append(f"{a['code']}: {a['id']} {a['detail'][:80]}")
    n = staged.get(TABLE, (0, []))[0]
    if n != len(result["rows"][TABLE]):
        fails.append(f"{TABLE}: {len(result['rows'][TABLE])} rows parsed but {n} in Parquet")
    if lake:
        for _, files in staged.values():
            for f in files:
                if lake.size(parsed_key(f, run_dir, env)) != f.stat().st_size:
                    fails.append(f"Parquet missing or wrong size in bucket: {parsed_key(f, run_dir, env)}")
    fails += roundtrip_mismatches(result["rows"], staged)
    if result["stopped"]:
        warns.append(result["stopped"])
    return {"fails": fails, "warns": warns}


def landing_count(run_id: str, env: str) -> int:
    cur = snowflake_cursor(env)
    cur.execute(f"select count(*) from FIGHTGRADE_{env.upper()}.LANDING.LND_{TABLE.upper()} "
                f"where _object_key like %s", (f"%part-{run_id}-%",))
    return cur.fetchone()[0]


def load_landing_cli(env: str) -> bool:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(["build", "--select", DBT_SELECT, "--target", env,
                              "--project-dir", str(ROOT / "dbt"), "--profiles-dir", str(Path("~/.dbt").expanduser())])
    return bool(res.success)


def finish(env: str, run_id: str, result: dict, lake: Lake | None, load=load_landing_cli) -> dict:
    """A run's pages and rows -> the lake -> LANDING, each step checked. `load(env)` runs the
    LANDING load (the CLI's dbt, or a Dagster op's). Without a lake (a dry run) nothing is
    uploaded. Returns the report; fails in it."""
    run_dir = RUNS / run_id
    shutil.rmtree(run_dir / "tables", ignore_errors=True)
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = stage(result["rows"], run_dir, run_id)
    if lake:
        n_pages, n_files = upload(lake, result, staged, run_dir, env)
        log(f"uploaded {n_pages} pages and {n_files} Parquet files")
    checks = verify(result, staged, run_dir, env, lake)
    report = {"run_id": run_id, "env": env, **result["report"], **checks}
    if lake and not checks["fails"] and staged:
        report["dbt_success"] = load(env)
        n, got = staged[TABLE][0], landing_count(run_id, env)
        report["landing"] = {"parquet": n, "landing": got}
        if got != n:
            report["fails"].append(f"LANDING {TABLE}: {got} rows loaded, {n} expected")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, indent=1))
    return report


def todo_flagged(env: str, lake: Lake | None, limit: int = 0) -> list[dict]:
    """The flagged headlines whose page is not archived yet, newest first."""
    from .news_classify import MODEL, PROMPT_VERSION

    todo = select(env, False, MODEL, PROMPT_VERSION)
    if lake:
        have = archived(lake, env)
        todo = [r for r in todo if page_name(r["news_url_id"]) not in have]
    return todo[:limit] if limit else todo


def main() -> int:
    from .news_classify import MODEL, PROMPT_VERSION

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = ap.add_mutually_exclusive_group(required=True)
    which.add_argument("--gold", action="store_true", help="the gold set's candidates")
    which.add_argument("--flagged", action="store_true", help=f"headlines {MODEL} {PROMPT_VERSION} flags as {', '.join(FLAG_TYPES)}")
    which.add_argument("--ids", type=Path, help="a JSON list of {news_url_id, site, url}")
    which.add_argument("--replay", action="store_true", help="re-parse this zone's archived pages; no requests")
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL)
    ap.add_argument("--max-requests", type=int, default=BACKFILL_BUDGET, help="per site")
    ap.add_argument("--max-minutes", type=float, default=MAX_FETCH_MINUTES)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.interval < MIN_INTERVAL:
        ap.error(f"--interval below {MIN_INTERVAL} s breaks the news crawl's rule")

    env, run_id = environment(), new_run_id()
    log(f"run {run_id} env={env} zone='{zone(env)}'")
    lake = None if args.dry_run and not args.replay else Lake(env)
    if args.replay:
        result = replay(lake, env, run_id)
        lake = None if args.dry_run else lake
    else:
        todo = json.loads(args.ids.read_text()) if args.ids else select(env, args.gold, MODEL, PROMPT_VERSION)
        if lake:
            have = archived(lake, env)
            todo = [r for r in todo if page_name(r["news_url_id"]) not in have]
        todo = todo[: args.limit] if args.limit else todo
        result = run(env, run_id, lake, todo, args.interval, args.max_requests, args.max_minutes)
    report = finish(env, run_id, result, lake)
    log("report:\n" + json.dumps({**report, "warns": report["warns"][:20]}, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
