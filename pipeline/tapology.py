"""Tapology crawl: newly completed events -> raw HTML and Parquet in the lake -> LANDING.

    .venv/bin/python -m pipeline.tapology                         # DEV: crawl, upload, load, check
    .venv/bin/python -m pipeline.tapology --limit 10 --dry-run    # crawl and stage locally only

The method is the archive's, unchanged (docs/scraping-method.md): headed real
Chrome clears Cloudflare once, then every page is a same-origin fetch() inside
that page at >= 2 s pacing. Rows are built by the same extractor and flatten()
that scrapers/driver/reparse.py uses, so a new partition has exactly the shape
of the 820k rows already in LANDING.

Only COMPLETED events are kept. Discovery is /fightcenter?schedule=results,
newest first, read back to the first page with nothing new on it, minus every
event already archived or queued. The listing stops at 15 pages (~two weeks),
so a weekly sweep of the sitemap queues what it cannot reach. Title bouts on
those cards also get their bout page, which is what bout_details is built from.

Polite by construction: one browser, one page at a time, no retry loops, and
at most DAILY_BUDGET requests a run. Work the budget cannot reach is queued in
the lake for the next run, never dropped. A Cloudflare challenge
mid-run gets a minute of quiet and one ordinary page load, at most twice; a 503
or 429 ends the run at once. Either way the crawl stops instead of hammering — a
block that is kept warm never clears (HANDOFF.stage2.md §3).
"""
import argparse, gzip, importlib.util, json, os, re, select, shutil, subprocess, sys, tempfile, time
from collections import Counter
from contextlib import contextmanager
from datetime import date, timedelta
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
# Each event on the results listing sits in a block with id "preview<event id>". The
# sidebar (featured events, the pick'em deadline) links upcoming events too, which
# would queue them from a page that lists only finished ones.
LISTED_EVENT = re.compile(r"""id=['"]preview(\d+)['"]""")
# qa() codes that mean a page did not parse into a usable record. Everything else
# it reports (counts_disagree, no_promotion, ...) is a known property of real
# Tapology data and is surfaced as a warning. That includes fighter_without_slug:
# Tapology lists some fighters with no profile page (fighter_linked: false), and
# the bulk archive holds 882 such bouts.
FATAL_QA = {"parse_failed", "bout_not_two_fighters"}
BACKOFF = (15, 45)          # seconds, for a soft failure (503, unavailable page)
# Pacing. The archive's "1.0 s floor" was measured in August with VPN rotation
# spreading load across IPs. On 2026-10-26, from one IP, ~13 requests at ~1.3 s
# drew a Cloudflare challenge; at 3 s the same pages fetched clean. From Dagster+
# Serverless (AWS us-west-2 IPs), 2.5 s drew a challenge every ~25 requests across
# three runs, and 4 s drew none in 100 on an IP that had just been challenged
# twice. 4 s fits ~420 requests in MAX_FETCH_MINUTES, and a peak Saturday
# (~270 pages) in ~20 minutes.
DEFAULT_SLEEP = 4.0
RECLEAR_WAIT = 60           # seconds of quiet before re-loading a challenged page
MAX_RECLEARS = 2            # per run; past that, stop rather than push
# The run's budget. The VPN crawl measured ~530 events per IP at 1.0 s before
# Tapology's first 503, and rotated proactively at 450 (HANDOFF.md §8). There is
# no rotation here, so 450 is what one run may spend, discovery and retries
# included. A peak Saturday is ~268 pages. What a run cannot fetch waits in the
# queue for the next one. A backfill also keeps a per-IP daily ledger (ip_budget).
DAILY_BUDGET = 450
MAX_FETCH_MINUTES = 30      # stop fetching by then: the run, and the PROD build after it, end by ~02:40
RATE_LIMITED = (429, 503)   # per-IP: waiting does nothing (HANDOFF.md §8), so stop and defer
MAX_TRIES = 7               # days an event may stay unfinished in the queue before it is dropped
DISCOVERY_SHARE = 3         # discovery may spend at most 1/3 of a run's budget
RESUME_OVERLAP = 6          # results pages a day's new events can push the listing down, and then some
LISTING_PAGES = 15          # the results listing stops there: every later page repeats page 15
# The listing reaches back ~375 events (two weeks). The sitemap reaches everything:
# 4,000 event ids per sub-sitemap, each entry with a <lastmod>. A weekly sweep of
# the recent-id sub-sitemaps queues what the listing cannot: results entered late
# for older events, and the gap since the bulk archive (its last full day below).
SWEEP_EVERY_DAYS = 7
BULK_ARCHIVE_DATE = "2026-08-21"
SITEMAP_INDEX = f"{BASE}/sitemap.xml"
SITEMAP_ENTRY = re.compile(r"<loc>[^<]*/fightcenter/events/(\d+)-([A-Za-z0-9-]+)</loc>\s*<lastmod>([0-9-]+)</lastmod>")
# c3.JS rejects bodies under 5 kB as Tapology's "Temporarily Unavailable" page; an
# empty sub-sitemap is 110 bytes, so sitemaps get their own fetch.
SITEMAP_JS = r"""
async (u) => {
  try {
    const r = await fetch(u, {credentials:"include"});
    const b = await r.text();
    if (/Just a moment/i.test(b)) return {status:"CHALLENGE", html:null};
    return {status:r.status, html: r.ok ? b : null};
  } catch (e) { return {status:"FAIL", html:null}; }
}
"""
BOUT_LOOKBACK_DAYS = 30     # title bouts loaded this recently with no bout page are retried
BUDGET_KEY = "state/tapology/ip_budget.json"     # zone-less: DEV and PROD runs share the IP
LOCK_PREFIX = "state/tapology/ip_lock/"           # one run per egress IP at a time
LOCK_STALE_HOURS = 3


def queue_key(env: str) -> str:
    return f"{zone(env)}state/tapology/queue.json"


class Budget:
    """Requests this run may spend, and its fetch deadline. `why` names what set the
    limit, for the Stop: the run's own cap, a backfill's daily ledger, another run."""

    def __init__(self, limit: int, minutes: float = MAX_FETCH_MINUTES, why: str = "request budget spent"):
        self.limit, self.spent, self.why, self.minutes = limit, 0, why, minutes
        self.deadline = time.time() + minutes * 60

    def take(self) -> None:
        if self.spent >= self.limit:
            raise Stop(f"{self.why} ({self.limit} requests)")
        if time.time() > self.deadline:
            raise Stop(f"fetch deadline reached ({self.minutes:g} min)")
        self.spent += 1


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

    def __init__(self, pw, budget: Budget):
        self.pw, self.ctx, self.page, self.profile = pw, None, None, None
        self.budget = budget
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
        self.budget.take()
        self.page.goto(f"{BASE}/fightcenter", wait_until="domcontentloaded", timeout=60000)
        for _ in range(15):                      # a clear lands in 6-9 s
            if "Just a moment" not in self.page.title():
                return True
            time.sleep(2)
        return False

    def fetch(self, url: str, js: str) -> dict:
        self.budget.take()
        return self.page.evaluate(js, url)

    def reclear(self, url: str) -> bool:
        """After a mid-run challenge: back off, then load the page once as a normal
        visitor would and let the browser clear it. Never interacts with it."""
        time.sleep(RECLEAR_WAIT)
        self.budget.take()
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


class Stop(Exception):
    """End the run's fetching now; what is left waits in the queue for tomorrow."""


class Blocked(Stop):
    """Cloudflare challenged us mid-run: stop, don't push."""


def polite_fetch(br: Browser, url: str, js: str, sleep: float) -> tuple[str | None, str]:
    """(html, status). Soft failures get two backed-off retries, then give up. A
    challenge gets one quiet re-clear (at most MAX_RECLEARS per run), then stops;
    a 503 or 429 is this IP's rate limit and stops at once."""
    for attempt in range(len(BACKOFF) + 1):
        res = br.fetch(url, js)
        time.sleep(sleep)
        if res.get("html"):
            return res["html"], "ok"
        status = res.get("status")
        if status == "CHALLENGE":
            if br.reclears >= MAX_RECLEARS:
                raise Blocked(f"Cloudflare challenge at {url}")
            br.reclears += 1
            log(f"  challenge on {url.rsplit('/', 1)[-1][:50]} -> quiet {RECLEAR_WAIT}s, re-clear "
                f"{br.reclears}/{MAX_RECLEARS}")
            if not br.reclear(url):
                raise Blocked(f"Cloudflare challenge at {url}")
            continue
        if status in (404, 410, "REDIRECT"):
            return None, str(status)
        if status in RATE_LIMITED:
            raise Stop(f"rate limited ({status}) at {url}")
        if attempt < len(BACKOFF):
            log(f"  soft {status} on {url.rsplit('/', 1)[-1][:50]} -> wait {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
    return None, f"gave_up_{status}"


def listed_events(html: str) -> list[str]:
    """Event slugs in the listing's own results, in page order, without the sidebar's."""
    links = list(dict.fromkeys(EVENT_LINK.findall(html)))
    ids = set(LISTED_EVENT.findall(html))
    if not ids:                     # the markup changed: take every link rather than find nothing
        log("  results page has no preview blocks; taking every event link on it")
        return links
    return [s for s in links if s.split("-", 1)[0] in ids]


def discover(br: Browser, accounted: set[str], sleep: float, cap: int, resume_page: int | None
             ) -> tuple[list[str], int | None, Stop | None]:
    """Event slugs on the results listing (newest first) that are neither archived
    nor queued, read back to the frontier: the first page with nothing new on it.

    Returns them, the page to resume from tomorrow if `cap` pages (or the run's
    budget) ran out before the frontier, and that Stop if it was the budget. The
    listing shifts down as events are added, so the resumed scan starts two pages
    early and walks over up to RESUME_OVERLAP already-seen pages before an empty
    page counts as the frontier again; anything seen twice is de-duplicated."""
    found: list[str] = []
    read, stopped = 0, None

    def scan(first: int, overlap: int = 0) -> int | None:
        nonlocal read, stopped
        n, found_here, empty = first, 0, 0
        while True:
            if n > LISTING_PAGES:
                return None
            if read >= cap:
                return n
            url = f"{BASE}/fightcenter?schedule=results" + (f"&page={n}" if n > 1 else "")
            read += 1
            try:
                html, status = polite_fetch(br, url, c3.JS, sleep)
            except Stop as e:
                stopped = e
                return n
            if html is None:
                log(f"  results page {n}: {status}")
                return None if status in ("404", "410") else n      # past the end, or retry tomorrow
            on_page = listed_events(html)
            if not on_page:
                return None                                         # past the end of the listing
            new = [s for s in on_page if s not in accounted and s not in found]
            log(f"  results page {n}: {len(on_page)} events, {len(new)} not yet archived or queued")
            found.extend(new)
            found_here += len(new)
            empty = 0 if new else empty + 1
            if not new and (found_here or empty > overlap):
                return None
            n += 1

    resume = scan(1)
    if resume is not None:                  # cut short near the top: tomorrow rescans it from page 1,
        return found, resume_page or resume, stopped    # so keep any deeper point still owed
    if resume_page:
        log(f"  resuming yesterday's unfinished discovery near page {resume_page}")
        resume = scan(max(resume_page - 2, 2), overlap=RESUME_OVERLAP)
    return found, resume, stopped


def sweep(br: Browser, accounted: set[str], since: str, id_floor: int, sleep: float
          ) -> tuple[list[str], bool, Stop | None]:
    """Events whose sitemap entry changed on or after `since`, with an id at or above
    `id_floor`, that are neither archived nor queued — newest id first. Reads the
    index, then the event sub-sitemaps from the one holding `id_floor` up to the
    first empty one: ~5 requests. Also returns whether every sub-sitemap was read
    (if not, the sweep is due again next run) and the Stop that cut it short."""
    found: list[str] = []
    try:
        index, status = polite_fetch(br, SITEMAP_INDEX, SITEMAP_JS, sleep)
        if index is None:
            log(f"  sitemap index: {status}")
            return [], False, None
        subs = re.findall(r"<loc>([^<]*/events/sitemap(?:_(\d+))?\.xml)</loc>", index)
        for url, n in sorted(subs, key=lambda u: int(u[1] or 1)):
            if int(n or 1) * 4000 <= id_floor:      # sub-sitemap n holds ids [(n-1)*4000, n*4000)
                continue
            xml, status = polite_fetch(br, url, SITEMAP_JS, sleep)
            if xml is None:
                log(f"  sitemap {url.rsplit('/', 1)[-1]}: {status}")
                return found, False, None
            entries = SITEMAP_ENTRY.findall(xml)
            new = [f"{i}-{name}" for i, name, mod in entries
                   if int(i) >= id_floor and mod >= since and f"{i}-{name}" not in accounted]
            log(f"  sitemap {url.rsplit('/', 1)[-1]}: {len(entries)} events, {len(new)} changed since "
                f"{since} and not archived or queued")
            found += new
            if not entries:
                break
        return sorted(set(found), key=lambda slug: -int(slug.split("-")[0])), True, None
    except Stop as e:
        return found, False, e


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


def crawl(limit: int, sleep: float, budget: Budget, env: str, run_dir: Path, known: set[str],
          state: dict, pending_bouts: list, discover_events: bool = True,
          sweep_from: tuple[str, int] | None = None) -> dict:
    """Fetch, archive locally and parse, until the work or the budget runs out.

    Order: events new on the listing (newest first), then the queue earlier runs
    could not finish, then title-bout pages, older runs' first. Whatever is left
    goes back in the queue (`state`, saved to the lake once this run's pages are
    uploaded), so a short day defers work and never drops it. An event that is
    not finished yet (results not entered) is retried daily for MAX_TRIES days,
    counted from its date: an upcoming one (the listing's sidebar links featured
    events) waits in the queue until then without spending a try.
    With discover_events=False only `state`'s queue and `pending_bouts` are
    fetched, and the saved queue is left as it is (the leftovers come back as
    "leftover"). `sweep_from` (since, id_floor) also sweeps the sitemap, queued
    after everything else."""
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

    queue = [q for q in state.get("queue", []) if q["slug"] not in known]
    abandoned = set(state.get("abandoned", []))
    resume = state.get("resume_page")
    last_sweep = state.get("last_sweep")
    worklist: list[dict] = [] if discover_events else list(queue)
    done: set[str] = set()                      # fetched this run, whatever the outcome
    retry: list[dict] = []

    def defer(item: dict, why: str) -> None:
        tries = item.get("tries", 0) + 1
        if tries >= MAX_TRIES:
            abandoned.add(item["slug"])
            report["events_dropped_after_retries"] += 1
            log(f"  dropping {item['slug'][:60]} after {tries} tries ({why})")
        else:
            retry.append({"slug": item["slug"], "tries": tries})

    def archive(entity: str, slug: str, html: str) -> str:
        key = raw_html_key("tapology", entity, fetch_date, slug, env)
        local = raw_dir / entity / f"{slug}.html.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(local, "wb", compresslevel=6) as f:     # archive BEFORE loading
            f.write(html.encode("utf-8"))
        pages.append((local, key))
        return key

    with sync_playwright() as pw:
        br = Browser(pw, budget)
        try:
            if not br.open():
                raise Blocked("Cloudflare did not clear on the first page")
            log("browser ready, Cloudflare cleared")
            if discover_events:
                accounted = known | abandoned | {q["slug"] for q in queue}
                new, resume, stop = discover(br, accounted, sleep, max(budget.limit // DISCOVERY_SHARE, 1), resume)
                worklist = [{"slug": s, "tries": 0} for s in new] + queue
                log(f"{len(new)} events new on the listing, {len(queue)} queued by earlier runs")
                if stop:
                    raise stop
                if sweep_from:
                    swept, complete, stop = sweep(br, accounted | set(new), *sweep_from, sleep)
                    worklist += [{"slug": s, "tries": 0} for s in swept]
                    last_sweep = fetch_date if complete else last_sweep
                    log(f"{len(swept)} more events from the sitemap" + ("" if complete else ", sweep unfinished"))
                    if stop:
                        raise stop

            title_bouts = list(pending_bouts)
            for item in worklist:
                if limit and report["events_kept"] >= limit:
                    break
                if item.get("after", "") > fetch_date:  # not held yet; stays queued
                    continue
                slug = item["slug"]
                url = f"{BASE}/fightcenter/events/{slug}"
                html, status = polite_fetch(br, url, c3.JS, sleep)
                done.add(slug)
                if html is None:
                    report[f"event_miss_{status}"] += 1
                    if status in ("404", "410", "REDIRECT"):
                        abandoned.add(slug)                # gone from Tapology for good
                    else:
                        defer(item, status)
                    continue
                if "primaryDetailsContainer" not in html and "sectionFightCard" not in html:
                    report["event_bad_page"] += 1
                    defer(item, "bad_page")
                    continue
                try:
                    ev = parse_event(html, url)
                except Exception as ex:                    # a parser bug must not stop the crawl
                    report["event_parse_error"] += 1
                    anomalies.append({"slug": slug, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
                    defer(item, "parse_failed")
                    continue
                if ev.get("status") == "Cancelled":       # never gets results: no retries
                    report["event_skipped_Cancelled"] += 1
                    abandoned.add(slug)
                    continue
                if ev.get("status") != "Completed":       # completed events only; try again later
                    report[f"event_skipped_{ev.get('status')}"] += 1
                    when = c3.page_date(ev) or ""           # no readable date: spends tries
                    if when >= fetch_date:                  # today or later: no try spent yet
                        retry.append({"slug": slug, "tries": item.get("tries", 0), "after": when})
                    else:
                        defer(item, str(ev.get("status")))
                    continue
                key = archive("event", slug, html)
                e_row, b_rows, l_rows, qa = event_rows(ev, slug, key, fetch_date)
                anomalies += qa
                rows["event_details"].append(e_row)
                rows["bouts"] += b_rows
                rows["event_fighters"] += l_rows
                report["events_kept"] += 1
                log(f"  event {report['events_kept']:>3} {slug[:60]} "
                    f"({len(b_rows)} bouts, {e_row.get('event_date_index')})")
                title_bouts += [(b, e_row) for b in b_rows if b.get("title_bout") and b.get("bout_slug")]

            seen_bouts = set()
            for b, e_row in title_bouts:
                if b["bout_slug"] in seen_bouts:
                    continue
                seen_bouts.add(b["bout_slug"])
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
        except Stop as e:
            stopped = f"stopped: {e}"
            log(stopped)
        finally:
            br.close()

    left = [q for q in (worklist or queue) if q["slug"] not in done]
    queue_next = left + retry
    report.update(requests=budget.spent, queued_for_next_run=len(queue_next))
    log(f"{budget.spent} requests spent; {len(queue_next)} events queued for the next run")
    state_next = {"queue": queue_next, "abandoned": sorted(abandoned), "resume_page": resume,
                  "last_sweep": last_sweep}
    return {"fetch_date": fetch_date, "rows": rows, "pages": pages, "report": dict(report), "anomalies": anomalies,
            "stopped": stopped, "state": state_next if discover_events else None,
            "leftover": queue_next, "abandoned": sorted(abandoned)}


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
    """Events already archived, finished. DEV also skips its own earlier test
    crawls; PROD ignores DEV's, which are tests and never reach PROD."""
    zones = ("",) if env == "prod" else ("", "dev/")
    return lake.archived_slugs("tapology", "event", zones) - unfinished_events(env)


def unfinished_events(env: str) -> set[str]:
    """Events archived before they finished: the bulk crawl saved ~120 past events
    as 'Upcoming' or 'In Progress', so their results were never fetched. Only PROD
    LANDING holds the bulk load (DEV reads it too); crawls keep completed events only."""
    cur = snowflake_cursor(env)
    cur.execute("""select payload:event_slug::string from FIGHTGRADE_PROD.LANDING.LND_TAPOLOGY_EVENT_DETAILS
                   qualify row_number() over (partition by payload:event_slug order by _loaded_at desc) = 1
                     and payload:status::string <> 'Completed'""")
    return {r[0] for r in cur.fetchall()}


def egress_ip() -> str:
    """The address the site sees, which the lock and the ledger are kept against."""
    import urllib.request
    try:
        return urllib.request.urlopen("https://api.ipify.org", timeout=10).read().decode().strip()
    except OSError:
        return "unknown"


@contextmanager
def ip_budget(lake: Lake, max_requests: int, minutes: float = MAX_FETCH_MINUTES, daily: int | None = None,
              budget_key: str = BUDGET_KEY, lock_prefix: str = LOCK_PREFIX, site: str = "Tapology", say=None):
    """The run's request budget on its egress IP, recorded in the lake's ledger after
    the run whether it finished or not: a request spent is spent.

    Serverless runs leave through 3 shared IPs, so two runs at once can land on the
    same one. The first takes a lock on it; a second gets no budget and stops before
    its first request. A lock older than LOCK_STALE_HOURS is a run that died without
    releasing it.

    A daily run may spend `max_requests`, whatever the IP spent earlier today. A
    backfill passes `daily`, the per-IP cap its shards share: what the ledger holds
    for today comes off it, so a shard relaunched on the same IP stays within it.
    Each site keeps its own lock and ledger (`lock_prefix`, `budget_key`), and logs
    through its own `say`, which the orchestration points at the run's log."""
    ip, today = egress_ip(), date.today().isoformat()
    lock = f"{lock_prefix}{ip}.json"
    held = lake.create(lock, {"since": time.time()})
    if not held and (lake.age_seconds(lock) or 0) > LOCK_STALE_HOURS * 3600:
        lake.delete(lock)
        held = lake.create(lock, {"since": time.time()})
    spent = lake.read_json(budget_key, {}).get(ip, {}).get(today, 0)
    if not held:
        budget = Budget(0, minutes, "another run holds this egress IP")
    elif daily is None:
        budget = Budget(max_requests, minutes)
    else:
        budget = Budget(max(min(max_requests, daily - spent), 0), minutes, "daily budget spent")
    (say or log)(f"egress IP {ip}: {spent} {site} requests spent today"
        + ("" if daily is None else f" of the backfill's {daily}") + f", this run may spend {budget.limit}"
        + ("" if held else " (another run holds this IP)"))
    try:
        yield budget
    finally:
        if held:
            ledger = lake.read_json(budget_key, {})
            week_ago = (date.today() - timedelta(days=7)).isoformat()
            days = {d: n for d, n in ledger.get(ip, {}).items() if d > week_ago}
            days[today] = days.get(today, 0) + budget.spent
            ledger[ip] = days
            lake.write_json(budget_key, ledger)
            lake.delete(lock)


def daily_crawl(lake: Lake, env: str, run_dir: Path, limit: int, sleep: float, budget: Budget,
                known: set[str]) -> dict:
    """The scheduled run: today's events plus whatever earlier runs deferred."""
    state = lake.read_json(queue_key(env), {})
    since = (date.today() - timedelta(days=BOUT_LOOKBACK_DAYS)).isoformat()
    bouts = pending_title_bouts(env, since)
    queue, today = state.get("queue", []), date.today().isoformat()
    parked = sum(1 for q in queue if q.get("after", "") > today)
    log(f"{len(queue) - parked} events and {len(bouts)} title-bout pages deferred by earlier runs; "
        f"{parked} upcoming events wait for their date")
    sweep_from = None
    last = state.get("last_sweep")
    if not last or (date.today() - date.fromisoformat(last)).days >= SWEEP_EVERY_DAYS:
        since = (date.fromisoformat(last) - timedelta(days=1)).isoformat() if last else BULK_ARCHIVE_DATE
        sweep_from = (since, sweep_id_floor(env, since))
        log(f"sitemap sweep due: entries changed since {since}, ids from {sweep_from[1]:,}")
    return crawl(limit, sleep, budget, env, run_dir, known, state, bouts, sweep_from=sweep_from)


def sweep_id_floor(env: str, since: str) -> int:
    """Ids are assigned when an event is created, usually weeks before it happens: the
    lowest id of events dated in the month before `since`, less one sub-sitemap. An
    event announced longer ago than that still arrives through the listing."""
    cur = snowflake_cursor(env)
    cur.execute("""select min(payload:event_id::int) from FIGHTGRADE_PROD.LANDING.LND_TAPOLOGY_EVENT_DETAILS
                   where payload:event_date_index::date between dateadd(day, -30, %s::date) and %s::date""",
                (since, since))
    low = cur.fetchone()[0]
    return max((low or 0) - 4000, 0)


def backfill_key(env: str, name: str) -> str:
    return f"{zone(env)}state/tapology/backfill/{name}.json"


def unfinished_until(env: str, until: str) -> list[str]:
    """Bulk-archived events dated by `until` that were saved before they finished."""
    cur = snowflake_cursor(env)
    cur.execute("""select payload:event_slug::string from FIGHTGRADE_PROD.LANDING.LND_TAPOLOGY_EVENT_DETAILS
                   qualify row_number() over (partition by payload:event_slug order by _loaded_at desc) = 1
                     and payload:status::string <> 'Completed' and payload:event_date_index::date <= %s::date""",
                (until,))
    return [r[0] for r in cur.fetchall()]


def plan_backfill(lake: Lake, env: str, until: str, known: set[str], sleep: float, budget: Budget) -> dict:
    """The gap since the bulk archive, as one fixed list that shards split: every
    sitemap entry changed since BULK_ARCHIVE_DATE that is not archived, plus the
    bulk's unfinished events dated by `until`. Fetches ~7 sitemaps, no events.

    The sitemap carries no dates, so events after `until` (and upcoming ones) are
    in the plan too: a shard keeps every completed event it fetches and hands the
    rest back as leftovers for the daily queue."""
    from playwright.sync_api import sync_playwright

    floor = sweep_id_floor(env, BULK_ARCHIVE_DATE)
    with sync_playwright() as pw:
        br = Browser(pw, budget)
        try:
            if not br.open():
                raise Blocked("Cloudflare did not clear on the first page")
            swept, complete, stop = sweep(br, known, BULK_ARCHIVE_DATE, floor, sleep)
        finally:
            br.close()
    if not complete:
        raise RuntimeError(f"sitemap sweep unfinished ({stop or 'a sub-sitemap failed'}); no plan written")
    extra = [e for e in unfinished_until(env, until) if e not in set(swept)]
    plan = {"until": until, "created": date.today().isoformat(), "since": BULK_ARCHIVE_DATE,
            "id_floor": floor, "slugs": swept + extra}
    lake.write_json(backfill_key(env, "plan"), plan)
    log(f"backfill plan: {len(swept)} events from the sitemap + {len(extra)} unfinished bulk events "
        f"= {len(plan['slugs'])}, in {backfill_key(env, 'plan')}")
    return {"fetch_date": plan["created"], "rows": {t: [] for t in TABLES}, "pages": [], "anomalies": [],
            "report": {"planned": len(plan["slugs"]), "requests": budget.spent}, "stopped": None}


def backfill_shard(lake: Lake, env: str, run_dir: Path, sleep: float, budget: Budget, known: set[str],
                   shard: int, shards: int) -> dict:
    """Shard `shard` of `shards` of the saved plan: every shards-th event, minus what is
    archived by now, so a shard cut short by its IP's budget is simply run again."""
    plan = lake.read_json(backfill_key(env, "plan"), None)
    if plan is None:
        raise RuntimeError("no backfill plan: run with backfill_until first")
    mine = [s for i, s in enumerate(plan["slugs"]) if i % shards == shard]
    todo = [s for s in mine if s not in known]
    log(f"backfill shard {shard}/{shards}: {len(mine)} events in the plan, {len(todo)} not archived yet")
    return crawl(0, sleep, budget, env, run_dir, known, {"queue": [{"slug": s, "tries": 0} for s in todo]}, [],
                 discover_events=False)


def save_queue(lake: Lake, env: str, result: dict) -> None:
    """After the run's pages are in the lake, so nothing leaves the queue unarchived."""
    if result.get("state") is not None:
        lake.write_json(queue_key(env), result["state"])


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


def pending_title_bouts(env: str, since: str) -> list[tuple[dict, dict]]:
    """Title bouts LANDING holds (from a crawl since `since`) whose bout page was
    never fetched — a crawl stopped by its budget or a challenge — as (bout, event)."""
    cur = snowflake_cursor(env)
    cur.execute(MISSING_TITLE_BOUTS.replace("{ENV}", env.upper()), (since,))
    return [(json.loads(p), {"event_date_index": d, "status": st}) for p, d, st in cur.fetchall()]


def backfill_title_bouts(since: str, limit: int, sleep: float, budget: Budget, env: str, run_dir: Path) -> dict:
    """Only the missing title-bout pages since `since`, no new events."""
    todo = pending_title_bouts(env, since)[: limit or None]
    log(f"{len(todo)} title bouts in LANDING since {since} have no bout page")
    if not todo:
        fetch_date = date.today().isoformat()
        return {"fetch_date": fetch_date, "rows": {t: [] for t in TABLES}, "pages": [],
                "report": {}, "anomalies": [], "stopped": None}
    return crawl(0, sleep, budget, env, run_dir, set(), {}, todo, discover_events=False)


# Only this source's LANDING tables. A run that built all of `landing` would also
# load another crawler's new files, and two such loads at once insert them twice.
DBT_SELECT = "path:models/landing/tapology"


def load_landing_cli(env: str) -> bool:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(["build", "--select", DBT_SELECT, "--target", env,
                              "--project-dir", str(ROOT / "dbt"), "--profiles-dir", str(Path("~/.dbt").expanduser())])
    return bool(res.success)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="completed events to keep (0: as many as the budget allows)")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help=f"seconds between requests (default {DEFAULT_SLEEP})")
    ap.add_argument("--max-requests", type=int, default=DAILY_BUDGET,
                    help=f"requests this run may spend (default {DAILY_BUDGET})")
    ap.add_argument("--max-minutes", type=float, default=MAX_FETCH_MINUTES,
                    help=f"stop fetching after this many minutes (default {MAX_FETCH_MINUTES})")
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
    else:
        daily = DAILY_BUDGET if args.title_bouts_since else None     # a title-bout catch-up is a backfill
        with ip_budget(lake, args.max_requests, args.max_minutes, daily) as budget:
            if args.title_bouts_since:
                result = backfill_title_bouts(args.title_bouts_since, args.limit, args.sleep, budget, env, run_dir)
            else:
                result = daily_crawl(lake, env, run_dir, args.limit, args.sleep, budget, known)
    shutil.rmtree(run_dir / "tables", ignore_errors=True)      # staging is rebuilt whole
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = stage(result["rows"], run_dir, run_id)

    if not args.dry_run:
        n_pages, n_files = upload(lake, result, staged, run_dir, env)
        save_queue(lake, env, result)
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
            report["fails"].append(f"dbt build --select {DBT_SELECT} failed")

    (run_dir / "report.json").write_text(json.dumps(report, indent=1))
    log("report:\n" + json.dumps(report, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
