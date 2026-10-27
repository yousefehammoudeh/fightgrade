"""MMADecisions crawl: judges' decisions -> raw HTML in the lake.

    .venv/bin/python -m pipeline.mmadecisions                        # DEV: crawl and upload
    .venv/bin/python -m pipeline.mmadecisions --limit 2 --dry-run    # crawl locally only

Allowed for private, non-commercial use, crawled politely: one browser per IP
with requests at least MIN_INTERVAL apart, start to start (MMADecisions'
terms: ~2-3 s per IP, and parallel runs from several IPs are fine). The method
is the other crawlers' (pipeline/tapology.py, pipeline/ufcstats.py): headed
Chrome loads the event index once, then every page is a same-origin fetch()
inside that page, archived as the server sent it.

Six tables (scrapers/extract/mmadecisions_extract.py), parsed from the
archived pages: events, decisions, judge_rounds, judge_totals, media_scores,
fan_scores. `--replay` re-parses the latest copy of every archived page from
the bucket with no requests to MMADecisions: a parser change is replayed,
never re-crawled.

Discovery is /decisions-by-event/<year>/, one page per year back to 1995 (each
row: event, date, number of decisions). An event page lists its decisions.
Decisions are added to an event over the days after it, so the scheduled
run (weekly, Sunday) re-reads recent events whose count on the index has changed. An event's page is
archived last, once every decision on it is: an event in the archive is
complete as of its fetch, and a run cut short redoes that event.
"""
import argparse, gzip, json, os, shutil, sys, tempfile, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from .lake import Lake, environment, new_run_id, raw_html_key, zone
from .tapology import (Budget, Stop, ip_budget as shared_ip_budget, parsed_key, roundtrip_mismatches,
                       snowflake_cursor, stage, virtual_display)

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "_crawlruns" / "mmadecisions"
BASE = "https://mmadecisions.com"
INDEX = BASE + "/decisions-by-event/{}/"

sys.path.insert(0, str(ROOT / "scrapers" / "extract"))
from mmadecisions_extract import (index_years, parse_decision, parse_event, parse_event_decisions,  # noqa: E402
                                  parse_event_index)

TABLES = ("mmadecisions_events", "mmadecisions_decisions", "mmadecisions_judge_rounds",
          "mmadecisions_judge_totals", "mmadecisions_media_scores", "mmadecisions_fan_scores")
DBT_SELECT = "path:models/landing/mmadecisions"     # only this source's tables

# Pacing, start to start. A decision page takes ~1.7 s to render on their side,
# so the gap after a response is what is left of the interval, if anything.
MIN_INTERVAL = 2.5
FLOOR_INTERVAL = 2.0        # the low end of what MMADecisions agreed to
BACKOFF = (15, 45)
DAILY_BUDGET = 1000         # a run's requests, ~45 min at 2.5 s (a UFC weekend is ~30); a backfill's per-IP daily cap too
MAX_FETCH_MINUTES = 30
RATE_LIMITED = (429, 503)
MAX_TRIES = 7               # runs an event may stay unfinished before it is dropped (and reported)
RECHECK_DAYS = 14           # events this recent are re-read when their decision count changes
FIRST_YEAR = 1995
BUDGET_KEY = "state/mmadecisions/ip_budget.json"
LOCK_PREFIX = "state/mmadecisions/ip_lock/"
LAKE_THREADS = 8
PAGE_MARK = "MMADecisions.com"      # in every real page's footer; error pages lack it

JS = r"""
async ([u, mark]) => {
  try {
    const r = await fetch(u, {credentials: "include"});
    const b = await r.text();
    if (r.ok && !b.includes(mark)) return {status: "NOT_A_PAGE", html: null};
    return {status: r.status, html: r.ok ? b : null};
  } catch (e) { return {status: "FAIL", html: null}; }
}
"""


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def queue_key(env: str) -> str:
    return f"{zone(env)}state/mmadecisions/queue.json"


def backfill_key(env: str, name: str) -> str:
    return f"{zone(env)}state/mmadecisions/backfill/{name}.json"


class Browser:
    """Headed Chrome on the event index, fetching everything from inside it."""

    def __init__(self, pw, budget: Budget, interval: float):
        self.pw, self.ctx, self.page, self.profile, self.display = pw, None, None, None, None
        self.budget, self.interval, self.last = budget, interval, 0.0

    def open(self) -> str | None:
        """Load the current year's index as a visitor would; its HTML once loaded."""
        self.display = virtual_display()
        self.profile = tempfile.mkdtemp(prefix="mmad_chrome_")
        args = ["--disable-dev-shm-usage"] if sys.platform.startswith("linux") else []
        self.ctx = self.pw.chromium.launch_persistent_context(
            user_data_dir=self.profile, channel="chrome", headless=False, args=args, env=dict(os.environ),
            viewport={"width": 1280, "height": 900}, locale="en-US")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        url = INDEX.format(date.today().year)
        for attempt in range(len(BACKOFF) + 1):
            self.pace()
            try:
                resp = self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
                status = resp.status if resp else None
            except Exception as ex:          # an error status with no body: Chrome raises instead
                status = f"{type(ex).__name__}: {str(ex).splitlines()[0][:120]}"
            if status == 200 and PAGE_MARK in self.page.content():
                return self.page.content()
            if status in RATE_LIMITED:
                raise Stop(f"rate limited ({status}) on the event index")
            log(f"  event index: {status}" + (f" -> wait {BACKOFF[attempt]}s" if attempt < len(BACKOFF) else ""))
            if attempt < len(BACKOFF):
                time.sleep(BACKOFF[attempt])
        return None

    def pace(self) -> None:
        self.budget.take()
        wait = self.last + self.interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()

    def fetch(self, url: str) -> dict:
        self.pace()
        return self.page.evaluate(JS, [url, PAGE_MARK])

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


def polite_fetch(br: Browser, url: str) -> tuple[str | None, str]:
    """(html, status): two backed-off retries for a soft failure, a stop at once on
    429/503 (that IP is being told to slow down; waiting in place does not help)."""
    status = None
    for attempt in range(len(BACKOFF) + 1):
        res = br.fetch(url)
        if res.get("html"):
            return res["html"], "ok"
        status = res.get("status")
        if status in (404, 410):
            return None, str(status)
        if status in RATE_LIMITED:
            raise Stop(f"rate limited ({status}) at {url}")
        if attempt < len(BACKOFF):
            log(f"  soft {status} on {url.rsplit('/', 2)[-2]} -> wait {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
    return None, f"gave_up_{status}"


def crawl(limit: int, budget: Budget, interval: float, env: str, run_dir: Path, known: dict[str, set[str]],
          state: dict, discover: bool = True) -> dict:
    """Fetch and archive until the work or the budget runs out.

    discover=True (the daily run): the current year's index (and last year's in
    the first RECHECK_DAYS of January); every event on it not archived yet, and
    every recent one whose decision count differs from what was archived. Then
    `state`'s queue: events earlier runs did not finish. discover=False (a
    backfill shard): `state`'s queue only. Per event: its page, each decision
    not archived yet, then the event page into the archive."""
    from playwright.sync_api import sync_playwright

    fetch_date = date.today().isoformat()
    recent = (date.today() - timedelta(days=RECHECK_DAYS)).isoformat()
    raw_dir = run_dir / "raw"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps({"fetch_date": fetch_date, "env": env}))
    pages: list[tuple[Path, str]] = []
    report, anomalies, stopped = Counter(), [], None
    counts: dict[str, int] = dict(state.get("counts", {}))     # event id -> decisions archived with it
    worklist: list[dict] = list(state.get("queue", []))
    done: set[str] = set()
    retry: list[dict] = []

    def archive(entity: str, ident: str, html: str) -> str:
        key = raw_html_key("mmadecisions", entity, fetch_date, ident, env)
        local = raw_dir / entity / f"{ident}.html.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(local, "wb", compresslevel=6) as f:
            f.write(html.encode("utf-8"))
        pages.append((local, key))
        return key

    with sync_playwright() as pw:
        br = Browser(pw, budget, interval)
        try:
            current = br.open()
            if current is None:
                raise Stop("the event index did not load")
            log("browser ready on the event index")
            if discover:
                listed = parse_event_index(current)
                archive("event_index", str(date.today().year), current)
                if date.today() < date(date.today().year, 1, 1) + timedelta(days=RECHECK_DAYS):
                    last, status = polite_fetch(br, INDEX.format(date.today().year - 1))
                    if last:
                        archive("event_index", str(date.today().year - 1), last)
                        listed += parse_event_index(last)
                queued = {q["event_id"] for q in worklist}
                new = [e for e in listed if e["event_id"] not in queued and (
                       e["event_id"] not in known["event"]
                       or ((e["date"] or "") >= recent and counts.get(e["event_id"]) != e["n_decisions"]))]
                worklist = [{"event_id": e["event_id"], "slug": e["slug"], "n": e["n_decisions"]} for e in new] + worklist
                log(f"index: {len(listed)} events, {len(new)} new or changed; {len(queued)} queued by earlier runs")

            for item in worklist:
                if limit and report["events_kept"] >= limit:
                    break
                eid = item["event_id"]
                html, status = polite_fetch(br, f"{BASE}/event/{eid}/{item['slug']}")
                if html is None:
                    done.add(eid)
                    report[f"event_miss_{status}"] += 1
                    anomalies.append({"id": eid, "code": "event_missing", "detail": status})
                    continue
                decisions = parse_event_decisions(html)
                if item.get("n") is not None and len(decisions) != item["n"]:
                    anomalies.append({"id": eid, "code": "count_disagrees",
                                      "detail": f"index says {item['n']}, page lists {len(decisions)}"})
                missing = None
                for d in decisions:
                    if d["decision_id"] in known["decision"]:
                        continue
                    dhtml, dstatus = polite_fetch(br, f"{BASE}/decision/{d['decision_id']}/{d['slug']}")
                    if dhtml is None:
                        missing = f"decision {d['decision_id']}: {dstatus}"
                        if dstatus in ("404", "410"):
                            anomalies.append({"id": d["decision_id"], "code": "decision_missing", "detail": dstatus})
                            missing = None
                            continue
                        break
                    archive("decision", d["decision_id"], dhtml)
                    known["decision"].add(d["decision_id"])
                    report["decision_pages_kept"] += 1
                done.add(eid)
                if missing:                                 # redo the event next run
                    report["event_deferred"] += 1
                    if item.get("tries", 0) + 1 < MAX_TRIES:
                        retry.append({**item, "tries": item.get("tries", 0) + 1})
                    else:
                        anomalies.append({"id": eid, "code": "event_dropped", "detail": missing})
                    continue
                archive("event", eid, html)                 # last: the event is complete as of now
                counts[eid] = len(decisions)
                report["events_kept"] += 1
                log(f"  event {report['events_kept']:>4} {eid} {item['slug'][:50]} ({len(decisions)} decisions)")
        except Stop as e:
            stopped = f"stopped: {e}"
            log(stopped)
        finally:
            br.close()

    left = [q for q in worklist if q["event_id"] not in done] + retry
    rows, parse_failures = page_rows(pages)
    anomalies += parse_failures
    report.update(requests=budget.spent, queued_for_next_run=len(left))
    log(f"{budget.spent} requests spent; {report['decision_pages_kept']} decisions and {report['events_kept']} "
        f"events archived; {len(left)} events queued for the next run")
    return {"fetch_date": fetch_date, "pages": pages, "rows": rows, "report": dict(report), "anomalies": anomalies,
            "stopped": stopped, "state": {"queue": left, "counts": counts} if discover else None, "leftover": left}


def page_rows(pages: list[tuple[Path, str]]) -> tuple[dict[str, list], list[dict]]:
    """Archived event and decision pages -> the six tables' rows, and parse failures.
    Each row carries the page's bucket key and fetch_date. The crawl and --replay
    both come through here, so a replay gives exactly what the crawl did."""
    rows, anomalies = {t: [] for t in TABLES}, []
    for local, key in pages:
        entity, ident = key.split("/entity=", 1)[1].split("/", 1)[0], local.name[: -len(".html.gz")]
        if entity not in ("event", "decision"):
            continue
        fetch_date = key.split("/fetch_date=", 1)[1].split("/", 1)[0]
        html = gzip.decompress(local.read_bytes()).decode("utf-8")
        try:
            if entity == "event":
                out = {"mmadecisions_events": [parse_event(html, ident)]}
            else:
                d = parse_decision(html, ident)
                out = {"mmadecisions_decisions": [d["decision"]], "mmadecisions_judge_rounds": d["judge_rounds"],
                       "mmadecisions_judge_totals": d["judge_totals"], "mmadecisions_media_scores": d["media_scores"],
                       "mmadecisions_fan_scores": d["fan_scores"]}
        except Exception as ex:          # archived all the same; fix the parser and replay
            anomalies.append({"id": f"{entity}/{ident}", "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
            continue
        for table, recs in out.items():
            for r in recs:
                r["_raw_html_key"], r["fetch_date"] = key, fetch_date
            rows[table] += recs
    return rows, anomalies


def replay(lake: Lake, env: str, run_dir: Path) -> dict:
    """Re-parse the latest archived copy of every event and decision page in this
    environment's zone, straight from the bucket. No requests to MMADecisions."""
    latest: dict[tuple[str, str], str] = {}
    prefix = f"{zone(env)}raw-html/source=mmadecisions/"
    for blob in lake.bucket.list_blobs(prefix=prefix):
        entity = blob.name.split("/entity=", 1)[1].split("/", 1)[0]
        if entity in ("event", "decision") and blob.name.endswith(".html.gz"):
            ident = blob.name.rsplit("/", 1)[1][: -len(".html.gz")]
            if blob.name > latest.get((entity, ident), ""):          # fetch_date sorts as text
                latest[(entity, ident)] = blob.name
    log(f"replay: {sum(e == 'event' for e, _ in latest):,} event and "
        f"{sum(e == 'decision' for e, _ in latest):,} decision pages in {prefix}")

    def download(item):
        (entity, ident), key = item
        local = run_dir / "raw" / entity / f"{ident}.html.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        lake.bucket.blob(key).download_to_filename(str(local))
        return local, key

    with ThreadPoolExecutor(LAKE_THREADS * 2) as pool:
        pages = list(pool.map(download, sorted(latest.items())))
    rows, anomalies = page_rows(pages)
    log(f"replay: parsed into {', '.join(f'{len(r):,} {t}' for t, r in rows.items())}; "
        f"{len(anomalies)} pages failed to parse")
    return {"fetch_date": date.today().isoformat(), "pages": pages, "rows": rows, "anomalies": anomalies,
            "stopped": None, "state": None, "leftover": [], "replay": True,
            "report": {"pages_replayed": len(pages), **{f"rows_{t}": len(r) for t, r in rows.items()}}}


def known_pages(lake: Lake, env: str) -> dict[str, set[str]]:
    """Event and decision ids already archived. DEV also skips its own test crawls."""
    zones = ("",) if env == "prod" else ("", "dev/")
    return {e: lake.archived_slugs("mmadecisions", e, zones) for e in ("event", "decision")}


def plan_backfill(lake: Lake, env: str, interval: float, budget: Budget, known: dict[str, set[str]]) -> dict:
    """Every event on every year of the index, as one fixed list that shards split.
    The year pages are archived too: they carry each event's date and count."""
    from playwright.sync_api import sync_playwright

    fetch_date = date.today().isoformat()
    run_pages: list[tuple[Path, str]] = []
    events: list[dict] = []
    with sync_playwright() as pw:
        br = Browser(pw, budget, interval)
        try:
            current = br.open()
            if current is None:
                raise RuntimeError("the event index did not load; no plan written")
            years = sorted(set(index_years(current)) | {date.today().year}, reverse=True)
            for y in years:
                html = current if y == date.today().year else None
                if html is None:
                    html, status = polite_fetch(br, INDEX.format(y))
                    if html is None:
                        raise RuntimeError(f"index {y}: {status}; no plan written")
                events += [{"event_id": e["event_id"], "slug": e["slug"], "n": e["n_decisions"]}
                           for e in parse_event_index(html)]
                run_pages.append((y, html))
        finally:
            br.close()
    plan = {"created": fetch_date, "years": len(run_pages), "events": events}
    lake.write_json(backfill_key(env, "plan"), plan)
    todo = [e for e in events if e["event_id"] not in known["event"]]
    n_dec = sum(e["n"] or 0 for e in events)
    log(f"backfill plan: {len(events)} events in {len(run_pages)} years, {n_dec:,} decisions listed; "
        f"{len(todo)} events and ~{n_dec - len(known['decision']):,} decisions not archived yet")
    return {"fetch_date": fetch_date, "pages": [], "rows": {t: [] for t in TABLES}, "index_pages": run_pages,
            "anomalies": [], "stopped": None,
            "report": {"planned_events": len(events), "listed_decisions": n_dec, "events_to_fetch": len(todo),
                       "decisions_archived": len(known["decision"]), "requests": budget.spent},
            "state": None, "leftover": []}


def archive_index_pages(result: dict, run_dir: Path, env: str) -> None:
    """The plan's year pages into the run's archive, under event_index/<year>."""
    for y, html in result.pop("index_pages", []):
        key = raw_html_key("mmadecisions", "event_index", result["fetch_date"], str(y), env)
        local = run_dir / "raw" / "event_index" / f"{y}.html.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(local, "wb", compresslevel=6) as f:
            f.write(html.encode("utf-8"))
        result["pages"].append((local, key))


def backfill_shard(lake: Lake, env: str, run_dir: Path, interval: float, budget: Budget,
                   known: dict[str, set[str]], shard: int, shards: int) -> dict:
    """Shard `shard` of `shards` of the saved plan: every shards-th event not archived
    yet, with its decisions. A shard cut short is simply run again."""
    plan = lake.read_json(backfill_key(env, "plan"), None)
    if plan is None:
        raise RuntimeError("no backfill plan: run with backfill_plan first")
    mine = [e for i, e in enumerate(plan["events"]) if i % shards == shard]
    todo = [e for e in mine if e["event_id"] not in known["event"]]
    log(f"backfill shard {shard}/{shards}: {len(mine)} events, {len(todo)} not archived yet "
        f"(~{sum(e['n'] or 0 for e in todo):,} decisions)")
    return crawl(0, budget, interval, env, run_dir, known, {"queue": todo}, discover=False)


def ip_budget(lake: Lake, max_requests: int, minutes: float = MAX_FETCH_MINUTES, daily: int | None = None):
    """pipeline.tapology.ip_budget with MMADecisions' own lock and ledger: a daily run spends
    up to `max_requests`; a backfill passes `daily`, its shards' per-IP cap."""
    return shared_ip_budget(lake, max_requests, minutes, daily, BUDGET_KEY, LOCK_PREFIX, "MMADecisions")


def upload(lake: Lake, result: dict, staged: dict, run_dir: Path, env: str) -> tuple[int, int]:
    """Raw pages first (a replay's are in the bucket already), then the Parquet."""
    pages = [] if result.get("replay") else result["pages"]
    with ThreadPoolExecutor(LAKE_THREADS) as pool:
        list(pool.map(lambda p: lake.upload_file(p[0], p[1], "application/gzip"), pages))
    files = [f for _, fs in staged.values() for f in fs]
    for f in files:
        lake.upload_file(f, parsed_key(f, run_dir, env), "application/octet-stream")
    return len(pages), len(files)


def save_state(lake: Lake, env: str, result: dict, shard: int | None, run_id: str) -> None:
    """After the pages are in the lake, so nothing leaves the queue unarchived."""
    if result.get("state") is not None:
        lake.write_json(queue_key(env), result["state"])
    if shard is not None:
        lake.write_json(backfill_key(env, f"shard-{shard}"),
                        {"run_id": run_id, "leftover": result["leftover"], "anomalies": result["anomalies"][:500]})


def verify(result: dict, staged: dict, run_dir: Path, env: str, lake: Lake | None) -> dict:
    """Every archived page is valid gzip and, once uploaded, the same size in the
    bucket; every page parsed; the Parquet holds exactly the parsed rows."""
    fails, warns = [], []
    with ThreadPoolExecutor(LAKE_THREADS) as pool:
        in_bucket = dict(pool.map(lambda p: (p[1], lake.size(p[1])), result["pages"])) if lake else {}
    for local, key in result["pages"]:
        try:
            if b"MMADecisions.com" not in gzip.decompress(local.read_bytes()):
                fails.append(f"not an MMADecisions page: {key}")
        except OSError:
            fails.append(f"corrupt gzip: {local.name}")
        if lake and in_bucket[key] != local.stat().st_size:
            fails.append(f"raw page missing or wrong size in bucket: {key}")
    for a in result["anomalies"]:
        (fails if a["code"] == "parse_failed" else warns).append(f"{a['code']}: {a['id']} {a['detail'][:80]}")
    raw_keys = {k for _, k in result["pages"]}
    for table, recs in result["rows"].items():
        n = staged.get(table, (0, []))[0]
        if n != len(recs):
            fails.append(f"{table}: {len(recs)} rows parsed but {n} in Parquet")
        if {r["_raw_html_key"] for r in recs} - raw_keys:
            fails.append(f"{table}: rows point at an unarchived page")
    if lake:
        for _, files in staged.values():
            for f in files:
                if lake.size(parsed_key(f, run_dir, env)) != f.stat().st_size:
                    fails.append(f"Parquet missing or wrong size in bucket: {parsed_key(f, run_dir, env)}")
    fails += roundtrip_mismatches(result["rows"], staged)
    if result["stopped"]:
        warns.append(result["stopped"])
    return {"fails": fails, "warns": warns}


def landing_counts(run_id: str, staged: dict, env: str) -> dict:
    """Rows of this run's files that LANDING now holds, per table."""
    cur, out = snowflake_cursor(env), {}
    for table, (n, _) in staged.items():
        cur.execute(f"select count(*) from FIGHTGRADE_{env.upper()}.LANDING.LND_{table.upper()} "
                    f"where _object_key like %s", (f"%part-{run_id}-%",))
        out[table] = {"parquet": n, "landing": cur.fetchone()[0]}
    return out


def load_landing_cli(env: str) -> bool:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(["build", "--select", DBT_SELECT, "--target", env,
                              "--project-dir", str(ROOT / "dbt"), "--profiles-dir", str(Path("~/.dbt").expanduser())])
    return bool(res.success)


def run(env: str, run_id: str, lake: Lake, budget: Budget | None, interval: float, limit: int = 0,
        plan: bool = False, shard: int | None = None, shards: int = 0, do_replay: bool = False) -> dict:
    """One run of whichever kind, with its year pages archived."""
    run_dir = RUNS / run_id
    if do_replay:
        return replay(lake, env, run_dir)
    known = known_pages(lake, env)
    log(f"{len(known['event']):,} events and {len(known['decision']):,} decisions already archived")
    if plan:
        result = plan_backfill(lake, env, interval, budget, known)
        archive_index_pages(result, run_dir, env)
    elif shards:
        result = backfill_shard(lake, env, run_dir, interval, budget, known, shard, shards)
    else:
        result = crawl(limit, budget, interval, env, run_dir, known, lake.read_json(queue_key(env), {}))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="events to archive (0: as many as the budget allows)")
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL,
                    help=f"seconds between request starts (default {MIN_INTERVAL}, floor {FLOOR_INTERVAL})")
    ap.add_argument("--max-requests", type=int, default=DAILY_BUDGET)
    ap.add_argument("--daily-budget", type=int, default=DAILY_BUDGET,
                    help="a backfill's per-IP daily cap, which it may raise; a daily run has none")
    ap.add_argument("--max-minutes", type=float, default=MAX_FETCH_MINUTES)
    ap.add_argument("--dry-run", action="store_true", help="crawl and archive locally; upload nothing")
    ap.add_argument("--backfill-plan", action="store_true", help="write the all-events plan from the 32 year pages")
    ap.add_argument("--backfill-shard", type=int, metavar="K")
    ap.add_argument("--backfill-shards", type=int, default=0, metavar="N")
    ap.add_argument("--replay", action="store_true",
                    help="re-parse every archived page from the bucket and load LANDING; no requests to MMADecisions")
    args = ap.parse_args()
    if args.interval < FLOOR_INTERVAL:
        ap.error(f"--interval below {FLOOR_INTERVAL} s is faster than MMADecisions agreed to")

    env = environment()
    run_id = new_run_id()
    log(f"run {run_id} env={env} zone='{zone(env)}' -> {(RUNS / run_id).relative_to(ROOT)}")
    lake = Lake(env)
    run_dir = RUNS / run_id
    if args.replay:
        result = run(env, run_id, lake, None, args.interval, do_replay=True)
    else:
        daily = args.daily_budget if args.backfill_plan or args.backfill_shards else None
        with ip_budget(lake, args.max_requests, args.max_minutes, daily) as budget:
            result = run(env, run_id, lake, budget, args.interval, args.limit, args.backfill_plan,
                         args.backfill_shard, args.backfill_shards)
    shutil.rmtree(run_dir / "tables", ignore_errors=True)
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = stage(result["rows"], run_dir, run_id)
    backfill = args.backfill_plan or bool(args.backfill_shards)
    if not args.dry_run:
        n_pages, n_files = upload(lake, result, staged, run_dir, env)
        save_state(lake, env, result, args.backfill_shard if args.backfill_shards else None, run_id)
        log(f"uploaded {n_pages} raw pages and {n_files} Parquet files")
    checks = verify(result, staged, run_dir, env, None if args.dry_run else lake)
    report = {"run_id": run_id, "env": env, "fetch_date": result["fetch_date"], "crawl": result["report"],
              "rows": {t: len(r) for t, r in result["rows"].items()}, **checks}
    # A backfill loads LANDING once, after every shard; a replay or a daily run loads its own.
    if not args.dry_run and not backfill and not checks["fails"] and staged:
        report["dbt_success"] = load_landing_cli(env)
        report["landing"] = landing_counts(run_id, staged, env)
        for t, c in report["landing"].items():
            if c["parquet"] != c["landing"]:
                report["fails"].append(f"LANDING {t}: {c['landing']} rows loaded, {c['parquet']} expected")
        if not report["dbt_success"]:
            report["fails"].append(f"dbt build --select {DBT_SELECT} failed")
    (RUNS / run_id).mkdir(parents=True, exist_ok=True)
    (RUNS / run_id / "report.json").write_text(json.dumps(report, indent=1))
    log("report:\n" + json.dumps({**report, "warns": report["warns"][:20]}, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
