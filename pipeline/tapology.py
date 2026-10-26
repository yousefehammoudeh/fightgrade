"""Tapology crawl: newly completed events -> raw HTML and Parquet in the lake -> LANDING.

    .venv/bin/python -m pipeline.tapology --limit 10              # DEV: crawl, upload, load, check
    .venv/bin/python -m pipeline.tapology --limit 10 --dry-run    # crawl and stage locally only

The method is the archive's, unchanged (docs/scraping-method.md): headed real
Chrome clears Cloudflare once, then every page is a same-origin fetch() inside
that page at >= 2 s pacing. Rows are built by the same extractor and flatten()
that scrapers/driver/reparse.py uses, so a new partition has exactly the shape
of the 820k rows already in LANDING.

Only COMPLETED events are kept. Discovery is /fightcenter?schedule=results,
newest first, minus every event already archived in the lake. Title bouts on
those cards also get their bout page, which is what bout_details is built from.

Polite by construction: one browser, one page at a time, no retry loops. A
Cloudflare challenge mid-run gets a minute of quiet and one ordinary page load,
at most twice; after that the crawl stops instead of hammering — a block that
is kept warm never clears (HANDOFF.stage2.md §3).
"""
import argparse, gzip, importlib.util, json, os, re, select, shutil, subprocess, sys, tempfile, time
from collections import Counter
from datetime import date
from pathlib import Path

import duckdb

from .lake import (Lake, environment, json_rows_select, new_run_id, parquet_copy_options, raw_html_key,
                   write_json_rows, zone)

LAKE_FIELDS = ("_raw_html_key", "fetch_date")   # carried as Parquet columns, kept out of the payload

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "_crawlruns"
BASE = "https://www.tapology.com"

sys.path.insert(0, str(ROOT / "scrapers" / "extract"))
from tapology_extract import parse_bout_page, parse_event  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# flatten() / qa() / the fetch JS, exactly as the archive crawl and reparse use them.
c3 = _load("crawl_stage3", ROOT / "scrapers" / "driver" / "crawl_stage3.py")
c3b = _load("crawl_stage3b", ROOT / "scrapers" / "driver" / "crawl_stage3b.py")

TABLES = ("event_details", "bouts", "event_fighters", "bout_details")
EVENT_LINK = re.compile(r"/fightcenter/events/(\d+-[A-Za-z0-9-]+)")
# qa() codes that mean a page did not parse into a usable record. Everything else
# it reports (counts_disagree, no_promotion, ...) is a known property of real
# Tapology data and is surfaced as a warning.
FATAL_QA = {"parse_failed", "bout_not_two_fighters", "fighter_without_slug"}
BACKOFF = (15, 45)          # seconds, for a soft failure (503, unavailable page)
# Pacing. The archive's "1.0 s floor" was measured in August with VPN rotation
# spreading load across IPs. On 2026-10-26, from one IP, ~13 requests at ~1.3 s
# drew a Cloudflare challenge; at 3 s the same pages fetched clean. 2.5 s keeps a
# peak Saturday (~270 pages) near 12 minutes.
DEFAULT_SLEEP = 2.5
RECLEAR_WAIT = 60           # seconds of quiet before re-loading a challenged page
MAX_RECLEARS = 2            # per run; past that, stop rather than push


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def virtual_display() -> subprocess.Popen | None:
    """Headed Chrome needs a display. A Linux container (Dagster+ Serverless) has
    none, so start a virtual one with Xvfb. Headed-under-Xvfb is what cleared
    Cloudflare from a datacenter IP (experiments/xvfb-probe/RESULT.md); headless
    never does. On a desktop, the real display is used."""
    if not sys.platform.startswith("linux") or os.environ.get("DISPLAY"):
        return None
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise RuntimeError("no display and no Xvfb: this machine cannot run headed Chrome")
    # -displayfd: Xvfb picks a free display and writes its number to the pipe only
    # once it accepts connections. A fixed sleep raced Chrome on Serverless.
    ready_r, ready_w = os.pipe()
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen([xvfb, "-displayfd", str(ready_w), "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
                            pass_fds=(ready_w,), stdout=subprocess.DEVNULL, stderr=err)
    os.close(ready_w)
    number = b""
    with os.fdopen(ready_r, "rb", buffering=0) as ready:
        while not number.endswith(b"\n") and select.select([ready], [], [], 20)[0]:
            chunk = ready.read(16)
            if not chunk:                        # Xvfb exited before it was ready
                break
            number += chunk
    if not number.strip().isdigit():
        proc.kill()
        err.seek(0)
        raise RuntimeError(f"Xvfb did not start: {err.read().decode(errors='replace')[-2000:] or 'no output'}")
    os.environ["DISPLAY"] = f":{number.strip().decode()}"
    return proc


class Browser:
    """Headed Chrome holding Cloudflare's clearance for the whole run."""

    def __init__(self, pw):
        self.pw, self.ctx, self.page, self.profile = pw, None, None, None
        self.reclears = 0
        self.display = None

    def open(self) -> bool:
        self.display = virtual_display()
        self.profile = tempfile.mkdtemp(prefix="tap_chrome_")
        # A container's /dev/shm is tiny; without this Chrome crashes on big pages.
        args = ["--disable-dev-shm-usage"] if sys.platform.startswith("linux") else []
        # env explicitly: Chrome is started by Playwright's driver process, which
        # was spawned before virtual_display() set DISPLAY and never saw it.
        self.ctx = self.pw.chromium.launch_persistent_context(
            user_data_dir=self.profile, channel="chrome", headless=False, args=args, env=dict(os.environ),
            viewport={"width": 1280, "height": 900}, locale="en-US")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.goto(f"{BASE}/fightcenter", wait_until="domcontentloaded", timeout=60000)
        for _ in range(15):                      # a clear lands in 6-9 s
            if "Just a moment" not in self.page.title():
                return True
            time.sleep(2)
        return False

    def fetch(self, url: str, js: str) -> dict:
        return self.page.evaluate(js, url)

    def reclear(self, url: str) -> bool:
        """After a mid-run challenge: back off, then load the page once as a normal
        visitor would and let the browser clear it. Never interacts with it."""
        time.sleep(RECLEAR_WAIT)
        self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(10):
            if "Just a moment" not in self.page.title():
                return True
            time.sleep(2)
        return False

    def close(self) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self.profile:
                shutil.rmtree(self.profile, ignore_errors=True)
            if self.display:
                self.display.terminate()
                os.environ.pop("DISPLAY", None)


class Blocked(Exception):
    """Cloudflare challenged us mid-run: stop, don't push."""


def polite_fetch(br: Browser, url: str, js: str, sleep: float) -> tuple[str | None, str]:
    """(html, status). Soft failures get two backed-off retries, then give up. A
    challenge gets one quiet re-clear (at most MAX_RECLEARS per run), then stops."""
    for attempt in range(len(BACKOFF) + 1):
        res = br.fetch(url, js)
        time.sleep(sleep)
        if res.get("html"):
            return res["html"], "ok"
        status = res.get("status")
        if status == "CHALLENGE":
            if br.reclears >= MAX_RECLEARS:
                raise Blocked(url)
            br.reclears += 1
            log(f"  challenge on {url.rsplit('/', 1)[-1][:50]} -> quiet {RECLEAR_WAIT}s, re-clear "
                f"{br.reclears}/{MAX_RECLEARS}")
            if not br.reclear(url):
                raise Blocked(url)
            continue
        if status in (404, 410, "REDIRECT"):
            return None, str(status)
        if attempt < len(BACKOFF):
            log(f"  soft {status} on {url.rsplit('/', 1)[-1][:50]} -> wait {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
    return None, f"gave_up_{status}"


def discover(br: Browser, known: set[str], want: int, max_pages: int, sleep: float) -> list[str]:
    """Newest completed-event slugs we don't hold, from the results listing."""
    found: list[str] = []
    for n in range(1, max_pages + 1):
        url = f"{BASE}/fightcenter?schedule=results" + (f"&page={n}" if n > 1 else "")
        html, status = polite_fetch(br, url, c3.JS, sleep)
        if html is None:
            log(f"  results page {n}: {status}")
            break
        on_page = list(dict.fromkeys(EVENT_LINK.findall(html)))
        new = [s for s in on_page if s not in known and s not in found]
        log(f"  results page {n}: {len(on_page)} events, {len(new)} not yet archived")
        found += new
        if len(found) >= want:
            break
    return found


def event_rows(ev: dict, slug: str, key: str, fetch_date: str) -> tuple[dict, list, list, list]:
    """One completed event page -> its event_details, bouts and event_fighters rows."""
    idx = {"slug": slug, "event_id": int(slug.split("-")[0]),
           "date": "9999-12-31", "date_precision": "unknown", "promotion_id": None}
    e_row, b_rows, l_rows = c3.flatten(ev, idx)
    for r in [e_row, *b_rows, *l_rows]:
        r["_raw_html_key"], r["fetch_date"] = key, fetch_date
    return e_row, b_rows, l_rows, c3.qa(ev, idx)


def bout_row(bp: dict, b: dict, e_row: dict, key: str, fetch_date: str) -> tuple[dict, list]:
    """One title-bout page -> its bout_details row, keyed off the card row it came from."""
    idx = {"slug": b["bout_slug"], "bout_id": b["bout_id"], "event_slug": b["event_slug"],
           "date": e_row.get("event_date_index"), "status": e_row.get("status"),
           "sport": b.get("sport_base"), "title_bout": b.get("title_bout")}
    d = c3b.flatten(bp, idx)
    d["_raw_html_key"], d["fetch_date"] = key, fetch_date
    return d, c3b.qa(bp, idx)


def crawl(limit: int, sleep: float, max_pages: int, env: str, run_dir: Path, known: set[str]) -> dict:
    """Fetch, archive locally and parse. Returns what to stage and what happened."""
    from playwright.sync_api import sync_playwright

    fetch_date = date.today().isoformat()
    raw_dir = run_dir / "raw"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps({"fetch_date": fetch_date, "env": env}))
    rows = {t: [] for t in TABLES}
    pages: list[tuple[Path, str]] = []          # (local gz, bucket key)
    report = Counter()
    anomalies: list[dict] = []
    stopped = None

    def archive(entity: str, slug: str, html: str) -> str:
        key = raw_html_key("tapology", entity, fetch_date, slug, env)
        local = raw_dir / entity / f"{slug}.html.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(local, "wb", compresslevel=6) as f:     # archive BEFORE loading
            f.write(html.encode("utf-8"))
        pages.append((local, key))
        return key

    with sync_playwright() as pw:
        br = Browser(pw)
        try:
            if not br.open():
                raise Blocked("Cloudflare did not clear on the first page")
            log("browser ready, Cloudflare cleared")
            candidates = discover(br, known, limit * 2, max_pages, sleep)

            title_bouts = []
            for slug in candidates:
                if report["events_kept"] >= limit:
                    break
                url = f"{BASE}/fightcenter/events/{slug}"
                html, status = polite_fetch(br, url, c3.JS, sleep)
                if html is None:
                    report[f"event_miss_{status}"] += 1
                    continue
                if "primaryDetailsContainer" not in html and "sectionFightCard" not in html:
                    report["event_bad_page"] += 1
                    continue
                try:
                    ev = parse_event(html, url)
                except Exception as ex:                    # a parser bug must not stop the crawl
                    report["event_parse_error"] += 1
                    anomalies.append({"slug": slug, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
                    continue
                if ev.get("status") != "Completed":       # completed events only
                    report[f"event_skipped_{ev.get('status')}"] += 1
                    continue
                key = archive("event", slug, html)
                e_row, b_rows, l_rows, qa = event_rows(ev, slug, key, fetch_date)
                anomalies += qa
                rows["event_details"].append(e_row)
                rows["bouts"] += b_rows
                rows["event_fighters"] += l_rows
                report["events_kept"] += 1
                log(f"  event {report['events_kept']:>2}/{limit} {slug[:60]} "
                    f"({len(b_rows)} bouts, {e_row.get('event_date_index')})")
                title_bouts += [(b, e_row) for b in b_rows if b.get("title_bout") and b.get("bout_slug")]

            for b, e_row in title_bouts:
                url = f"{BASE}/fightcenter/bouts/{b['bout_slug']}"
                html, status = polite_fetch(br, url, c3b.JS, sleep)
                if html is None:
                    report[f"bout_miss_{status}"] += 1
                    continue
                if "boutComparisonTable" not in html and "Bout Information" not in html:
                    report["bout_bad_page"] += 1
                    continue
                key = archive("bout", b["bout_slug"], html)
                d, qa = bout_row(parse_bout_page(html, url), b, e_row, key, fetch_date)
                anomalies += qa
                rows["bout_details"].append(d)
                report["bout_pages_kept"] += 1
        except Blocked as e:
            stopped = f"stopped: Cloudflare challenge at {e}"
            log(stopped)
        finally:
            br.close()

    return {"fetch_date": fetch_date, "rows": rows, "pages": pages,
            "report": dict(report), "anomalies": anomalies, "stopped": stopped}


def rebuild(run_dir: Path, env: str) -> dict:
    """Re-parse a run's archived pages with no network — the archive's whole point:
    a pipeline bug after the fetch is fixed and replayed, never re-crawled."""
    meta = run_dir / "meta.json"
    some_page = next((run_dir / "raw").rglob("*.html.gz"))
    fetch_date = (json.loads(meta.read_text())["fetch_date"] if meta.exists()
                  else date.fromtimestamp(some_page.stat().st_mtime).isoformat())
    rows = {t: [] for t in TABLES}
    pages, anomalies, report = [], [], Counter()
    title_bouts = {}
    for local in sorted((run_dir / "raw" / "event").glob("*.html.gz")):
        slug = local.name[: -len(".html.gz")]
        ev = parse_event(gzip.decompress(local.read_bytes()).decode("utf-8"), f"{BASE}/fightcenter/events/{slug}")
        key = raw_html_key("tapology", "event", fetch_date, slug, env)
        e_row, b_rows, l_rows, qa = event_rows(ev, slug, key, fetch_date)
        pages.append((local, key))
        anomalies += qa
        rows["event_details"].append(e_row)
        rows["bouts"] += b_rows
        rows["event_fighters"] += l_rows
        report["events_kept"] += 1
        title_bouts.update({b["bout_slug"]: (b, e_row) for b in b_rows if b.get("title_bout") and b.get("bout_slug")})
    for local in sorted((run_dir / "raw" / "bout").glob("*.html.gz")):
        slug = local.name[: -len(".html.gz")]
        b, e_row = title_bouts[slug]
        key = raw_html_key("tapology", "bout", fetch_date, slug, env)
        html = gzip.decompress(local.read_bytes()).decode("utf-8")
        d, qa = bout_row(parse_bout_page(html, f"{BASE}/fightcenter/bouts/{slug}"), b, e_row, key, fetch_date)
        pages.append((local, key))
        anomalies += qa
        rows["bout_details"].append(d)
        report["bout_pages_kept"] += 1
    report["title_bouts_on_cards"] = len(title_bouts)
    return {"fetch_date": fetch_date, "rows": rows, "pages": pages,
            "report": dict(report), "anomalies": anomalies, "stopped": None}


def source_row(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in LAKE_FIELDS}


def stage(rows: dict, run_dir: Path, run_id: str) -> dict[str, tuple[int, list[Path]]]:
    """JSONL -> Parquet per table, in the parsed/ layout, with run-stamped names."""
    con = duckdb.connect()
    out = {}
    for table, recs in rows.items():
        if not recs:
            continue
        jsonl = run_dir / "tables" / f"{table}.jsonl"
        write_json_rows(jsonl, ((source_row(r), r["_raw_html_key"], r["fetch_date"]) for r in recs))
        dest = run_dir / "parsed" / f"entity={table}"
        dest.parent.mkdir(parents=True, exist_ok=True)   # DuckDB creates the leaf, not its parents
        con.execute(f"COPY ({json_rows_select(jsonl)}) TO '{dest}' ({parquet_copy_options(run_id)})")
        files = sorted(dest.rglob("*.parquet"))
        n = con.execute(f"SELECT count(*) FROM read_parquet({[str(f) for f in files]})").fetchone()[0]
        out[table] = (n, files)
    return out


def parsed_key(local: Path, run_dir: Path, env: str) -> str:
    return zone(env) + local.relative_to(run_dir).as_posix()


def roundtrip_mismatches(rows: dict, staged: dict) -> list[str]:
    """Rows whose JSON in the Parquet doesn't decode back to exactly what was parsed."""
    con, bad = duckdb.connect(), []
    for table, (_, files) in staged.items():
        stored = con.execute(f"SELECT _json, _raw_html_key, fetch_date::VARCHAR "
                             f"FROM read_parquet({[str(f) for f in files]})").fetchall()
        key = lambda row, k, d: json.dumps([row, k, d], sort_keys=True)
        got = sorted(key(json.loads(j), k, d) for j, k, d in stored)
        want = sorted(key(source_row(r), r["_raw_html_key"], r["fetch_date"]) for r in rows[table])
        if got != want:
            bad.append(f"{table}: Parquet JSON differs from the parsed rows")
    return bad


def verify(result: dict, staged: dict, run_dir: Path, env: str, lake: Lake | None, known: set[str]) -> dict:
    """Tests 4-6: the raw archive, parse quality and the Parquet, before anything loads."""
    fails, warns = [], []
    rows, pages = result["rows"], result["pages"]
    raw_keys = {k for _, k in pages}

    # 4. raw archive: every kept page is valid gzip and, once uploaded, the same size
    for local, key in pages:
        try:
            gzip.decompress(local.read_bytes())
        except OSError:
            fails.append(f"corrupt gzip: {local.name}")
        if lake and lake.size(key) != local.stat().st_size:
            fails.append(f"raw page missing or wrong size in bucket: {key}")

    # 5. parse quality
    for e in rows["event_details"]:
        if not e.get("parse_ok"):
            fails.append(f"parse_ok false: {e['event_slug']}")
        if e.get("status") != "Completed":
            fails.append(f"non-completed event kept: {e['event_slug']}")
        if e["event_slug"] in known:
            fails.append(f"event already archived was fetched again: {e['event_slug']}")
    for a in result["anomalies"]:
        (fails if a["code"] in FATAL_QA else warns).append(f"qa {a['code']}: {a['slug'][:50]} {a['detail'][:60]}")

    # 6. Parquet: row counts, keys that resolve, and an exact JSON round trip
    for table, recs in rows.items():
        n = staged.get(table, (0, []))[0]
        if n != len(recs):
            fails.append(f"{table}: {len(recs)} rows parsed but {n} in Parquet")
        missing = {r["_raw_html_key"] for r in recs} - raw_keys
        if missing:
            fails.append(f"{table}: {len(missing)} rows point at an unarchived page")
    if lake:
        for table, (_, files) in staged.items():
            for f in files:
                if lake.size(parsed_key(f, run_dir, env)) != f.stat().st_size:
                    fails.append(f"Parquet missing or wrong size in bucket: {parsed_key(f, run_dir, env)}")
    fails += roundtrip_mismatches(rows, staged)
    if result["stopped"]:
        warns.append(result["stopped"])
    return {"fails": fails, "warns": warns}


def known_events(lake: Lake, env: str) -> set[str]:
    """Events already archived. DEV also skips its own earlier test crawls; PROD
    ignores DEV's, which are tests and never reach PROD."""
    zones = ("",) if env == "prod" else ("", "dev/")
    return lake.archived_slugs("tapology", "event", zones)


def upload(lake: Lake, result: dict, staged: dict, run_dir: Path, env: str) -> tuple[int, int]:
    """Raw pages first, then the Parquet that points at them."""
    for local, key in result["pages"]:
        lake.upload_file(local, key, "application/gzip")
    n_files = 0
    for _, files in staged.values():
        for f in files:
            lake.upload_file(f, parsed_key(f, run_dir, env), "application/octet-stream")
            n_files += 1
    return len(result["pages"]), n_files


def snowflake_cursor(env: str):
    """As DBT_<ENV>_SVC: the key from a Dagster+ environment variable (one line of
    base64, as orchestration/dbt_profiles/profiles.yml reads it), else the local file."""
    import base64, os
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization

    kw = dict(account=os.environ.get("SNOWFLAKE_ACCOUNT", "FNMYVFY-FP38730"), user=f"DBT_{env.upper()}_SVC",
              authenticator="SNOWFLAKE_JWT", warehouse="FIGHTGRADE_WH")
    inline = os.environ.get(f"DBT_{env.upper()}_SVC_PRIVATE_KEY")
    if inline:
        der = serialization.load_der_private_key(base64.b64decode(inline), password=None)
        kw["private_key"] = der.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption())
    else:
        kw["private_key_file"] = str(Path(f"~/.snowflake/keys/dbt_{env}_svc.p8").expanduser())
    return snowflake.connector.connect(**kw).cursor()


def landing_counts(run_id: str, staged: dict, env: str) -> dict:
    """Test 7: rows of this run's files that LANDING now holds, per table."""
    cur, out = snowflake_cursor(env), {}
    for table, (n, _) in staged.items():
        cur.execute(f"select count(*) from FIGHTGRADE_{env.upper()}.LANDING.LND_TAPOLOGY_{table.upper()} "
                    f"where _object_key like %s", (f"%part-{run_id}-%",))
        out[table] = {"parquet": n, "landing": cur.fetchone()[0]}
    return out


MISSING_TITLE_BOUTS = """
select b.payload, e.payload:event_date_index::string, e.payload:status::string
from FIGHTGRADE_{ENV}.LANDING.LND_TAPOLOGY_BOUTS b
join FIGHTGRADE_{ENV}.LANDING.LND_TAPOLOGY_EVENT_DETAILS e on e.payload:event_slug = b.payload:event_slug
-- ::string, because a JSON null inside a VARIANT is not SQL NULL
where b.payload:title_bout::string is not null and b.payload:bout_slug::string is not null
  and b._partition_date >= %s and e.payload:status::string = 'Completed'
  and not exists (select 1 from FIGHTGRADE_{ENV}.LANDING.LND_TAPOLOGY_BOUT_DETAILS d
                  where d.payload:bout_slug = b.payload:bout_slug)
qualify row_number() over (partition by b.payload:bout_slug order by b._loaded_at desc) = 1
order by 2 desc
"""


def backfill_title_bouts(since: str, limit: int, sleep: float, env: str, run_dir: Path) -> dict:
    """Title bouts LANDING holds (from a crawl since `since`) whose bout page we never
    fetched — e.g. a crawl stopped by a challenge — fetched now."""
    from playwright.sync_api import sync_playwright

    cur = snowflake_cursor(env)
    cur.execute(MISSING_TITLE_BOUTS.replace("{ENV}", env.upper()), (since,))
    todo = [(json.loads(p), {"event_date_index": d, "status": st}) for p, d, st in cur.fetchall()][:limit]
    log(f"{len(todo)} title bouts in LANDING since {since} have no bout page")

    fetch_date = date.today().isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps({"fetch_date": fetch_date, "env": env, "mode": "title_bouts"}))
    rows = {t: [] for t in TABLES}
    pages, anomalies, report, stopped = [], [], Counter(), None
    if not todo:
        return {"fetch_date": fetch_date, "rows": rows, "pages": pages,
                "report": dict(report), "anomalies": anomalies, "stopped": None}
    with sync_playwright() as pw:
        br = Browser(pw)
        try:
            if not br.open():
                raise Blocked("Cloudflare did not clear on the first page")
            for b, e_row in todo:
                url = f"{BASE}/fightcenter/bouts/{b['bout_slug']}"
                html, status = polite_fetch(br, url, c3b.JS, sleep)
                if html is None or ("boutComparisonTable" not in html and "Bout Information" not in html):
                    report[f"bout_miss_{status}"] += 1
                    continue
                key = raw_html_key("tapology", "bout", fetch_date, b["bout_slug"], env)
                local = run_dir / "raw" / "bout" / f"{b['bout_slug']}.html.gz"
                local.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(local, "wb", compresslevel=6) as f:
                    f.write(html.encode("utf-8"))
                pages.append((local, key))
                d, qa = bout_row(parse_bout_page(html, url), b, e_row, key, fetch_date)
                anomalies += qa
                rows["bout_details"].append(d)
                report["bout_pages_kept"] += 1
                log(f"  bout page {report['bout_pages_kept']}/{len(todo)} {b['bout_slug'][:70]}")
        except Blocked as e:
            stopped = f"stopped: Cloudflare challenge at {e}"
            log(stopped)
        finally:
            br.close()
    return {"fetch_date": fetch_date, "rows": rows, "pages": pages,
            "report": dict(report), "anomalies": anomalies, "stopped": stopped}


def load_landing_cli(env: str) -> bool:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(["build", "--select", "landing", "--target", env,
                              "--project-dir", str(ROOT / "dbt"), "--profiles-dir", str(Path("~/.dbt").expanduser())])
    return bool(res.success)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=10, help="completed events to keep")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help=f"seconds between requests (default {DEFAULT_SLEEP})")
    ap.add_argument("--max-pages", type=int, default=3, help="results pages to scan for new events")
    ap.add_argument("--dry-run", action="store_true", help="crawl and stage locally; upload and load nothing")
    ap.add_argument("--from-run", metavar="RUN_ID",
                    help="re-parse that run's archived pages instead of crawling (no network to Tapology)")
    ap.add_argument("--title-bouts-since", metavar="YYYY-MM-DD",
                    help="instead of new events, fetch missing bout pages for title bouts LANDING holds since then")
    args = ap.parse_args()
    if args.sleep < 2.0:
        ap.error("--sleep below 2.0 draws Cloudflare challenges from one IP (see DEFAULT_SLEEP)")

    env = environment()
    run_id = args.from_run or new_run_id()
    run_dir = RUNS / run_id
    log(f"run {run_id} env={env} zone='{zone(env)}' -> {run_dir.relative_to(ROOT)}")

    lake = Lake(env)
    known = known_events(lake, env)
    log(f"{len(known):,} events already archived")

    if args.from_run:
        result = rebuild(run_dir, env)
        known -= {e["event_slug"] for e in result["rows"]["event_details"]}   # this run's own pages
        log(f"rebuilt {result['report']} from {run_dir.relative_to(ROOT)}/raw")
    elif args.title_bouts_since:
        result = backfill_title_bouts(args.title_bouts_since, args.limit, args.sleep, env, run_dir)
    else:
        result = crawl(args.limit, args.sleep, args.max_pages, env, run_dir, known)
    shutil.rmtree(run_dir / "tables", ignore_errors=True)      # staging is rebuilt whole
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = stage(result["rows"], run_dir, run_id)

    if not args.dry_run:
        n_pages, n_files = upload(lake, result, staged, run_dir, env)
        log(f"uploaded {n_pages} pages and {n_files} Parquet files")

    checks = verify(result, staged, run_dir, env, None if args.dry_run else lake, known)
    report = {"run_id": run_id, "env": env, "fetch_date": result["fetch_date"], "crawl": result["report"],
              "rows": {t: len(r) for t, r in result["rows"].items()}, **checks}
    if not args.dry_run and not checks["fails"] and staged:
        report["dbt_success"] = load_landing_cli(env)
        report["landing"] = landing_counts(run_id, staged, env)
        for t, c in report["landing"].items():
            if c["parquet"] != c["landing"]:
                report["fails"].append(f"LANDING {t}: {c['landing']} rows loaded, {c['parquet']} expected")
        if not report["dbt_success"]:
            report["fails"].append("dbt build --select landing failed")

    (run_dir / "report.json").write_text(json.dumps(report, indent=1))
    log("report:\n" + json.dumps(report, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
