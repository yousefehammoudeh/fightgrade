"""MMA news sitemaps: every article URL MMA Fighting and MMA Mania list -> the lake -> LANDING.

    .venv/bin/python -m pipeline.news_sitemaps                         # DEV: current and previous month
    .venv/bin/python -m pipeline.news_sitemaps --backfill              # DEV: every month not archived yet
    .venv/bin/python -m pipeline.news_sitemaps --site mmamania --dry-run
    .venv/bin/python -m pipeline.news_sitemaps --replay                # re-parse the archive, load LANDING
    .venv/bin/python -m pipeline.news_sitemaps --replay-dir DIR        # parse local XML; no upload

Both sites are Vox Media's. Their robots.txt disallows Claude's agents; the
owners agreed to an ongoing crawl (experiments/news/README.md). So: sitemaps
only here; an honest user agent naming the project; at least MIN_INTERVAL between request starts on a site;
one run per site at a time (a lock in the lake, whatever the egress IP); and
every response archived as served before anything is parsed from it.

Each site keeps one sitemap per month, /sitemaps/entries/<YYYY>/<M>. A URL
carries the article's publication day and id:

    https://www.mmafighting.com/2020/10/24/21532169/khabib-nurmagomedov-announces-retirement-...

Since the move to WordPress (2025-26) a URL has a section instead of the day
(/ufc/513670/slug), and some entries are profile pages, not articles
(/fighter/..., /fight/<a>-vs-<b>). <lastmod> is the last edit, not
publication; a month's sitemap follows it. Rows, one per <url>, go to LANDING
lnd_news_urls: site, url, section, article_id, url_date, slug, lastmod, sitemap_month.

The daily run reads each site's current and previous month (4 requests): a
month's sitemap grows until it ends, and an article can land a day late. The
backfill reads every month from the site's first that is not archived yet, so
one cut short is simply run again. A month that answers 404 has no sitemap (the
site's early months are sparse) and is recorded, not retried.
"""
import argparse, gzip, json, re, shutil, sys, time, urllib.error, urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .lake import Lake, environment, new_run_id, zone
from .tapology import Budget, Stop, parsed_key, roundtrip_mismatches, snowflake_cursor, stage

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "_crawlruns" / "news"

SITES = {
    "mmafighting": "https://www.mmafighting.com",
    "mmamania": "https://www.mmamania.com",
}
SITEMAP = "{base}/sitemaps/entries/{y}/{m}"
FIRST_MONTH = {"mmafighting": (2003, 12), "mmamania": (2005, 1)}   # earlier than either's first sitemap

# The project, by name; no contact, by the user's choice (2026-10-31).
USER_AGENT = "FightGrade-news/1.0 (private, non-commercial MMA research)"

TABLE = "news_urls"
DBT_SELECT = "path:models/landing/news"

MIN_INTERVAL = 6.0          # seconds between request starts on one site, the README's rule
TIMEOUT = 60
BACKOFF = (30, 90)          # a 5xx or a dropped connection, before trying the month again
RATE_LIMITED = (429,)       # stop the site at once: waiting on a limit keeps it warm
BLOCKED = (401, 403)        # the site refuses us: stop, and say so
DAILY_BUDGET = 20           # a daily run needs 4
BACKFILL_BUDGET = 700       # ~515 months across both sites, and their pages if any
MAX_FETCH_MINUTES = 90
MAX_INDEX_PAGES = 20        # a month's sitemap split into pages: never more than this
LOCK_STALE_HOURS = 3
NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

# Vox's URLs until the sites moved to WordPress carry the day and the id:
# /2020/10/24/21532169/slug. WordPress's carry a section and the id but no day:
# /ufc/513670/slug, /fight/511505/a-vs-b, /<event hub>/476215/slug; for those,
# lastmod stands in for the date downstream. `section` is the path before the id.
DATED_URL = re.compile(r"^https?://[^/]+/((?:[a-z0-9-]+/)*?)(\d{4})/(\d{1,2})/(\d{1,2})/(\d+)/([^/?#]+)/?$")
UNDATED_URL = re.compile(r"^https?://[^/]+/((?:[a-z0-9-]+/)*?)(\d{5,})/([^/?#]+)/?$")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def raw_xml_key(site: str, fetch_date: str, name: str, env: str | None = None) -> str:
    return f"{zone(env)}raw-xml/news/{site}/fetch_date={fetch_date}/{name}.xml.gz"


def month_name(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}"


def months(first: tuple[int, int], last: tuple[int, int]) -> list[tuple[int, int]]:
    out, (y, m) = [], first
    while (y, m) <= last:
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def recent_months(today: date) -> list[tuple[int, int]]:
    """The current month and the one before it."""
    prev = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
    return [prev, (today.year, today.month)]


class Fetcher:
    """One site's polite client: plain HTTP, the honest user agent, MIN_INTERVAL start to start."""

    def __init__(self, site: str, budget: Budget, interval: float):
        self.site, self.budget, self.interval, self.last = site, budget, interval, 0.0
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/xml, text/xml"}

    def get(self, url: str) -> tuple[int | str, bytes | None]:
        """(status, body). A transport error or timeout is 'FAIL'."""
        self.budget.take()
        wait = self.interval - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=self.headers), timeout=TIMEOUT) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, TimeoutError, OSError):
            return "FAIL", None

    def polite_get(self, url: str) -> tuple[int | str, bytes | None]:
        """A 5xx or a dropped connection is tried again after BACKOFF; a limit or a refusal stops the site."""
        for attempt in range(len(BACKOFF) + 1):
            status, body = self.get(url)
            if status == 200 or status == 404:
                return status, body
            if status in RATE_LIMITED:
                raise Stop(f"{self.site}: rate limited (HTTP {status})")
            if status in BLOCKED:
                raise Stop(f"{self.site}: refused (HTTP {status}); is this IP blocked?")
            if attempt == len(BACKOFF):
                return status, None
            log(f"  {self.site} {url.rsplit('/entries/', 1)[-1]}: {status} -> wait {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
        return "FAIL", None


def parse_sitemap(xml: bytes) -> tuple[str, list[dict]]:
    """('urlset', [{loc, lastmod}]) or ('sitemapindex', [{loc, lastmod}]). Raises on anything else."""
    root = ET.fromstring(xml)
    kind = root.tag.removeprefix(NS)
    if kind not in ("urlset", "sitemapindex"):
        raise ValueError(f"not a sitemap: <{root.tag}>")
    child = "url" if kind == "urlset" else "sitemap"
    out = []
    for el in root.iter(NS + child):
        loc = (el.findtext(NS + "loc") or "").strip()
        lastmod = (el.findtext(NS + "lastmod") or "").strip() or None
        if loc:
            out.append({"loc": loc, "lastmod": lastmod})
    return kind, out


def url_row(site: str, loc: str, lastmod: str | None, sitemap_month: str) -> dict:
    """One sitemap entry as a row. url_date is NULL when the URL carries no day."""
    row = {"site": site, "url": loc, "section": None, "article_id": None, "url_date": None, "slug": None,
           "lastmod": lastmod, "sitemap_month": sitemap_month}
    m = DATED_URL.match(loc)
    if m:
        section, y, mo, d, ident, slug = m.groups()
        row["section"] = section.strip("/") or None
        try:
            row["url_date"] = date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            pass
        row.update(article_id=ident, slug=slug)
        return row
    m = UNDATED_URL.match(loc)
    if m:
        row.update(section=m.group(1).strip("/") or None, article_id=m.group(2), slug=m.group(3))
    return row


def archived_months(lake: Lake, site: str, zones: tuple[str, ...]) -> set[str]:
    """Months with a sitemap (or a recorded 404) archived in any of the zones."""
    out = set()
    for z in zones:
        for blob in lake.bucket.list_blobs(prefix=f"{z}raw-xml/news/{site}/"):
            name = blob.name.rsplit("/", 1)[-1]
            if name.endswith((".xml.gz", ".404.json")):
                out.add(name[:7])
    return out


@contextmanager
def site_lock(lake: Lake, site: str):
    """One run per site at a time, whichever IP it leaves from. A lock older than
    LOCK_STALE_HOURS is a run that died without releasing it. Yields False when
    another run holds the site."""
    key = f"state/news/{site}/lock.json"
    held = lake.create(key, {"since": time.time()})
    if not held and (lake.age_seconds(key) or 0) > LOCK_STALE_HOURS * 3600:
        lake.delete(key)
        held = lake.create(key, {"since": time.time()})
    try:
        yield held
    finally:
        if held:
            lake.delete(key)


def crawl_site(site: str, plan: list[tuple[int, int]], budget: Budget, interval: float, env: str,
               run_dir: Path, fetch_date: str) -> dict:
    """Fetch and archive each planned month of one site; parse nothing yet."""
    f = Fetcher(site, budget, interval)
    pages, missing, anomalies, report = [], [], [], Counter()

    def archive(name: str, body: bytes) -> None:
        key = raw_xml_key(site, fetch_date, name, env)
        local = run_dir / "raw" / site / f"{name}.xml.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(local, "wb", compresslevel=6) as out:
            out.write(body)
        pages.append((local, key))

    stopped = None
    try:
        for y, m in plan:
            name = month_name(y, m)
            url = SITEMAP.format(base=SITES[site], y=y, m=m)
            status, body = f.polite_get(url)
            if status == 404:
                missing.append(name)
                report["months_404"] += 1
                continue
            if body is None:
                anomalies.append({"id": f"{site}/{name}", "code": "fetch_failed", "detail": str(status)})
                continue
            archive(name, body)                         # as served, before parsing
            report["months_kept"] += 1
            try:
                kind, entries = parse_sitemap(body)
            except (ET.ParseError, ValueError):
                continue                                # the parse below reports it
            if kind == "sitemapindex":                  # a month split into pages: follow its own links
                host = SITES[site].split("//", 1)[1]
                for i, e in enumerate(entries[:MAX_INDEX_PAGES], 1):
                    if e["loc"].split("//", 1)[-1].split("/", 1)[0] != host:
                        anomalies.append({"id": f"{site}/{name}", "code": "offsite_index_entry", "detail": e["loc"]})
                        continue
                    pstatus, pbody = f.polite_get(e["loc"])
                    if pbody is None:
                        anomalies.append({"id": f"{site}/{name}-p{i}", "code": "fetch_failed", "detail": str(pstatus)})
                        continue
                    archive(f"{name}-p{i}", pbody)
                    report["index_pages_kept"] += 1
            log(f"  {site} {name}: {len(entries)} {'pages' if kind == 'sitemapindex' else 'urls'}")
    except Stop as e:
        stopped = str(e)
        log(f"{site}: stopped: {e}")
    return {"pages": pages, "missing": missing, "anomalies": anomalies, "report": report, "stopped": stopped}


def page_rows(pages: list[tuple[Path, str]]) -> tuple[list[dict], list[dict], Counter]:
    """Archived sitemaps -> url rows, parse failures, counts. The crawl and a replay
    both come through here, so a replay gives exactly what the crawl did."""
    rows, anomalies, counts = [], [], Counter()
    for local, key in pages:
        site = key.split("raw-xml/news/", 1)[1].split("/", 1)[0]
        fetch_date = key.split("/fetch_date=", 1)[1].split("/", 1)[0]
        month = local.name[:7]
        try:
            kind, entries = parse_sitemap(gzip.decompress(local.read_bytes()))
        except (OSError, ET.ParseError, ValueError) as ex:
            anomalies.append({"id": f"{site}/{local.name}", "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
            continue
        if kind == "sitemapindex":
            continue
        counts[f"urls_listed_{site}"] += len(entries)
        for e in entries:
            r = url_row(site, e["loc"], e["lastmod"], month)
            if r["article_id"] is None:
                counts["urls_not_articles"] += 1
            elif r["url_date"] is None:
                counts["urls_undated"] += 1
            r["_raw_html_key"], r["fetch_date"] = key, fetch_date
            rows.append(r)
    return rows, anomalies, counts


def plan_for(lake: Lake | None, site: str, env: str, backfill: bool, today: date) -> list[tuple[int, int]]:
    """The daily run: the last two months. A backfill: every month not archived yet, plus those two."""
    recent = recent_months(today)
    if not backfill:
        return recent
    zones = ("",) if env == "prod" else ("", "dev/")
    have = archived_months(lake, site, zones) if lake else set()
    return [ym for ym in months(FIRST_MONTH[site], recent[-1]) if month_name(*ym) not in have or ym in recent]


def run(env: str, run_id: str, lake: Lake | None, sites: list[str], backfill: bool, interval: float,
        max_requests: int, max_minutes: float) -> dict:
    """Each site in turn, under its own lock; the rows parsed from what was archived."""
    fetch_date, run_dir = date.today().isoformat(), RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    budget = Budget(max_requests, max_minutes)
    pages, anomalies, report, stopped, missing = [], [], Counter(), [], {}
    for site in sites:
        plan = plan_for(lake, site, env, backfill, date.today())
        log(f"{site}: {len(plan)} months to read ({month_name(*plan[0])} .. {month_name(*plan[-1])})")
        with (site_lock(lake, site) if lake else nullcontext_true()) as held:
            if not held:
                stopped.append(f"{site}: another run holds the site's lock")
                continue
            got = crawl_site(site, plan, budget, interval, env, run_dir, fetch_date)
        pages += got["pages"]
        anomalies += got["anomalies"]
        report.update({f"{k}_{site}": v for k, v in got["report"].items()})
        missing[site] = got["missing"]
        if got["stopped"]:
            stopped.append(got["stopped"])
    rows, parse_failures, counts = page_rows(pages)
    anomalies += parse_failures
    report.update(counts, requests=budget.spent, rows=len(rows))
    log(f"{budget.spent} requests; {len(pages)} sitemaps archived; {len(rows):,} urls")
    return {"fetch_date": fetch_date, "run_dir": str(run_dir), "pages": pages, "rows": {TABLE: rows},
            "anomalies": anomalies, "missing": missing, "report": dict(report),
            "stopped": "; ".join(stopped) or None}


@contextmanager
def nullcontext_true():
    yield True


def replay(lake: Lake, env: str, run_id: str) -> dict:
    """Re-parse the latest archived copy of every month (and its pages) in this
    environment's zone, straight from the bucket. No requests to the sites: a
    parser change is replayed, and PROD can be filled from a copy of DEV's archive."""
    latest: dict[tuple[str, str], str] = {}
    prefix = f"{zone(env)}raw-xml/news/"
    for blob in lake.bucket.list_blobs(prefix=prefix):
        if blob.name.endswith(".xml.gz"):
            site, name = blob.name[len(prefix):].split("/", 1)[0], blob.name.rsplit("/", 1)[1][: -len(".xml.gz")]
            if blob.name > latest.get((site, name), ""):          # fetch_date sorts as text
                latest[(site, name)] = blob.name
    run_dir, pages = RUNS / run_id, []
    for (site, name), key in sorted(latest.items()):
        local = run_dir / "raw" / site / f"{name}.xml.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        lake.bucket.blob(key).download_to_filename(str(local))
        pages.append((local, key))
    rows, anomalies, counts = page_rows(pages)
    log(f"replay: {len(pages)} sitemaps in {prefix} -> {len(rows):,} urls")
    return {"fetch_date": date.today().isoformat(), "run_dir": str(run_dir), "pages": pages, "rows": {TABLE: rows},
            "anomalies": anomalies, "missing": {}, "report": {**counts, "sitemaps_replayed": len(pages),
            "rows": len(rows)}, "stopped": None, "replay": True}


def replay_dir(src: Path, env: str, run_id: str) -> dict:
    """Parse sitemaps saved under src/<site>/<YYYY-MM>.xml(.gz), as if just fetched. No requests."""
    fetch_date, run_dir = date.today().isoformat(), RUNS / run_id
    pages = []
    for p in sorted(src.glob("*/*.xml*")):
        site, name = p.parent.name, p.name.split(".xml", 1)[0]
        body = p.read_bytes()
        body = gzip.decompress(body) if p.suffix == ".gz" else body
        local = run_dir / "raw" / site / f"{name}.xml.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(local, "wb") as out:
            out.write(body)
        pages.append((local, raw_xml_key(site, fetch_date, name, env)))
    rows, anomalies, counts = page_rows(pages)
    return {"fetch_date": fetch_date, "run_dir": str(run_dir), "pages": pages, "rows": {TABLE: rows},
            "anomalies": anomalies, "missing": {}, "report": {**counts, "rows": len(rows)}, "stopped": None,
            "replay": True}


def upload(lake: Lake, result: dict, staged: dict, run_dir: Path, env: str) -> tuple[int, int]:
    """Raw XML first, then the Parquet, so no row points at an unarchived sitemap.
    A 404 month is recorded as an empty marker, so a backfill does not ask again."""
    for local, key in [] if result.get("replay") else result["pages"]:
        lake.upload_file(local, key, "application/gzip")
    for site, names in result.get("missing", {}).items():
        for name in names:
            lake.write_json(raw_xml_key(site, result["fetch_date"], name, env).replace(".xml.gz", ".404.json"),
                            {"status": 404})
    files = [f for _, fs in staged.values() for f in fs]
    for f in files:
        lake.upload_file(f, parsed_key(f, run_dir, env), "application/octet-stream")
    return len(result["pages"]), len(files)


def verify(result: dict, staged: dict, run_dir: Path, env: str, lake: Lake | None) -> dict:
    """Every archived sitemap is valid gzip and, once uploaded, the same size in the
    bucket; every one parsed; the Parquet holds exactly the parsed rows."""
    fails, warns = [], []
    for local, key in result["pages"]:
        try:
            gzip.decompress(local.read_bytes())
        except OSError:
            fails.append(f"corrupt gzip: {local}")
        if lake and lake.size(key) != local.stat().st_size:
            fails.append(f"raw sitemap missing or wrong size in bucket: {key}")
    for a in result["anomalies"]:
        (fails if a["code"] == "parse_failed" else warns).append(f"{a['code']}: {a['id']} {a['detail'][:80]}")
    raw_keys = {k for _, k in result["pages"]}
    recs = result["rows"][TABLE]
    n = staged.get(TABLE, (0, []))[0]
    if n != len(recs):
        fails.append(f"{TABLE}: {len(recs)} rows parsed but {n} in Parquet")
    if {r["_raw_html_key"] for r in recs} - raw_keys:
        fails.append(f"{TABLE}: rows point at an unarchived sitemap")
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", choices=sorted(SITES), action="append", help="default: both")
    ap.add_argument("--backfill", action="store_true", help="every month not archived yet")
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL)
    ap.add_argument("--max-requests", type=int, default=0, help=f"default {DAILY_BUDGET}, or {BACKFILL_BUDGET} for a backfill")
    ap.add_argument("--max-minutes", type=float, default=MAX_FETCH_MINUTES)
    ap.add_argument("--dry-run", action="store_true", help="fetch and archive locally; upload nothing")
    ap.add_argument("--replay", action="store_true",
                    help="re-parse the latest archived copy of every month and load LANDING; no requests")
    ap.add_argument("--replay-dir", type=Path, help="parse saved sitemaps (<dir>/<site>/<YYYY-MM>.xml); no requests")
    args = ap.parse_args()
    if args.interval < MIN_INTERVAL:
        ap.error(f"--interval below {MIN_INTERVAL} s breaks the news crawl's rule")

    env, run_id = environment(), new_run_id()
    run_dir = RUNS / run_id
    log(f"run {run_id} env={env} zone='{zone(env)}' -> {run_dir.relative_to(ROOT)}")
    lake = None if args.dry_run else Lake(env)
    if args.replay_dir:
        result = replay_dir(args.replay_dir, env, run_id)
        lake = None                                 # local files: nothing of theirs belongs in the lake
    elif args.replay:
        result = replay(lake or Lake(env), env, run_id)
    else:
        if args.dry_run and args.backfill:
            log("dry run: the backfill cannot see the archive, so it plans every month")
        result = run(env, run_id, lake, args.site or sorted(SITES), args.backfill, args.interval,
                     args.max_requests or (BACKFILL_BUDGET if args.backfill else DAILY_BUDGET), args.max_minutes)
    shutil.rmtree(run_dir / "tables", ignore_errors=True)
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = stage(result["rows"], run_dir, run_id)
    if lake:
        n_pages, n_files = upload(lake, result, staged, run_dir, env)
        log(f"uploaded {0 if result.get('replay') else n_pages} sitemaps and {n_files} Parquet files")
    checks = verify(result, staged, run_dir, env, lake)
    report = {"run_id": run_id, "env": env, "fetch_date": result["fetch_date"], "crawl": result["report"],
              "missing": result["missing"], **checks}
    if lake and not checks["fails"] and staged:
        report["dbt_success"] = load_landing_cli(env)
        n = staged[TABLE][0]
        report["landing"] = {"parquet": n, "landing": landing_count(run_id, env)}
        if report["landing"]["landing"] != n:
            report["fails"].append(f"LANDING {TABLE}: {report['landing']['landing']} rows loaded, {n} expected")
        if not report["dbt_success"]:
            report["fails"].append(f"dbt build --select {DBT_SELECT} failed")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, indent=1))
    log("report:\n" + json.dumps({**report, "warns": report["warns"][:20]}, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
