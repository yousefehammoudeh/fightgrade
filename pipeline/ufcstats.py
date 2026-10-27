"""UFC Stats crawl: newly completed UFC events -> raw HTML and Parquet in the lake -> LANDING.

    .venv/bin/python -m pipeline.ufcstats                         # DEV: crawl, upload, load, check
    .venv/bin/python -m pipeline.ufcstats --limit 2 --dry-run     # crawl and stage locally only

UFC Stats answers every plain HTTP client (requests, curl) with a "Checking
your browser…" JavaScript page, and the only access we are allowed is through
a real browser. So the method is Tapology's (pipeline/tapology.py): headed
Chrome loads the events list once and the page's own script clears the check
(~1 s; it sets an HttpOnly cookie), then every page is a same-origin fetch()
inside that page. Nothing here solves or replays the check.

Discovery is /statistics/events/completed, newest first. Its top row is the
NEXT event, so an event is kept only when every bout on its card has a result.
Each kept event brings its fight pages (totals, per round, sig strikes by
target and position) and the fighter page of everyone on it, refreshed so
records stay current. Page 1 holds ~25 events; `?page=all` (one request, every
event) is read only when page 1 has no event we already have. Events dated on
or before `since` (default: the last event in the 2026-02/03 export) are left
alone, so the first run fills the gap since that export, not all of history.

An event is all or nothing: if any of its fight pages cannot be fetched, none
of it is kept and it goes back in the queue. Polite like Tapology: one browser,
one page at a time at a measured pace (DEFAULT_SLEEP), DAILY_BUDGET requests a day per IP, the run stops on a 429
or 503, and what is left waits in the lake's queue for the next run.
"""
import argparse, gzip, json, os, shutil, sys, tempfile, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from .lake import Lake, environment, new_run_id, raw_html_key, zone
from .tapology import (Budget, Stop, egress_ip, parsed_key, roundtrip_mismatches, snowflake_cursor, stage,
                       virtual_display)

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "_crawlruns" / "ufcstats"
BASE = "http://www.ufcstats.com"
LISTING = f"{BASE}/statistics/events/completed"
FIGHTER_LISTING = f"{BASE}/statistics/fighters?char={{}}&page=all"

sys.path.insert(0, str(ROOT / "scrapers" / "extract"))
from ufcstats_extract import (CHALLENGE_MARK, parse_event, parse_events_list, parse_fight,  # noqa: E402
                              parse_fighter, parse_fighters_list)

TABLES = ("ufcstats_events", "ufcstats_fights", "ufcstats_fight_totals", "ufcstats_fight_rounds",
          "ufcstats_sig_strikes_by_target", "ufcstats_sig_strikes_by_position", "ufcstats_fighters")
# The newest event in ~/ufc-elo-calculator/seeder_data/data/ufcstats_events.csv.
EXPORT_LAST_EVENT = "2025-12-13"
BACKOFF = (15, 45)
# Pacing, measured 2026-10-26 from a home IP through headed Chrome: 160 fight
# pages at 2 s, 1 s and 0.5 s gaps drew no 429, 503 or browser check, and the
# server's ~0.5 s latency did not move. It is nginx with no rate-limit headers.
# 1 s is half the fastest clean rate, ~40 pages a minute: a UFC Saturday (~45
# requests: event, ~14 fights, ~28 fighters) takes ~70 s, the gap since the
# export ~40 min. Unmeasured from Dagster+ Serverless IPs; the stop on a
# 429/503 or a second browser check still applies.
DEFAULT_SLEEP = 1.0
MIN_SLEEP = 0.5             # the fastest gap measured clean; never go below it
RECLEAR_WAIT = 60
MAX_RECLEARS = 2
DAILY_BUDGET = 1500         # ~30 min at DEFAULT_SLEEP: the whole gap in one night
MAX_FETCH_MINUTES = 30
RATE_LIMITED = (429, 503)
MAX_TRIES = 7
SETTLE_DAYS = 7             # an event this far past is kept even with a bout lacking a result
BUDGET_KEY = "state/ufcstats/ip_budget.json"     # its own ledger: a different site from Tapology
LOCK_PREFIX = "state/ufcstats/ip_lock/"
LOCK_STALE_HOURS = 3
LAKE_THREADS = 8            # a backfill shard uploads ~5,600 raw pages; one at a time is ~15 min

JS = r"""
async ([u, mark]) => {
  try {
    const r = await fetch(u, {credentials: "include"});
    const b = await r.text();
    if (b.includes(mark)) return {status: "CHALLENGE", html: null};
    return {status: r.status, html: r.ok ? b : null};
  } catch (e) { return {status: "FAIL", html: null}; }
}
"""


def queue_key(env: str) -> str:
    return f"{zone(env)}state/ufcstats/queue.json"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


class Blocked(Stop):
    """The browser check came back mid-run and did not clear: stop, don't push."""


class Browser:
    """Headed Chrome holding UFC Stats' browser-check cookie for the whole run."""

    def __init__(self, pw, budget: Budget):
        self.pw, self.ctx, self.page, self.profile = pw, None, None, None
        self.budget = budget
        self.reclears = 0
        self.display = None

    def _cleared(self, tries: int) -> bool:
        """The page's own script runs the check and reloads; wait for real content."""
        for _ in range(tries):
            try:
                if self.page.locator("a[href*='-details/']").count():
                    return True
            except Exception:                    # the page navigated mid-query
                pass
            time.sleep(1)
        return False

    def open(self) -> str | None:
        """Load the events list as a visitor would. Returns its HTML once cleared:
        it is also discovery's page 1."""
        self.display = virtual_display()
        self.profile = tempfile.mkdtemp(prefix="ufcstats_chrome_")
        args = ["--disable-dev-shm-usage"] if sys.platform.startswith("linux") else []
        self.ctx = self.pw.chromium.launch_persistent_context(
            user_data_dir=self.profile, channel="chrome", headless=False, args=args, env=dict(os.environ),
            viewport={"width": 1280, "height": 900}, locale="en-US")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.budget.take()
        self.page.goto(LISTING, wait_until="domcontentloaded", timeout=60000)
        return self.page.content() if self._cleared(30) else None

    def fetch(self, url: str) -> dict:
        self.budget.take()
        return self.page.evaluate(JS, [url, CHALLENGE_MARK])

    def reclear(self) -> bool:
        """After a mid-run check: back off, then load the events list once and let
        the browser clear it. Never interacts with it."""
        time.sleep(RECLEAR_WAIT)
        self.budget.take()
        self.page.goto(LISTING, wait_until="domcontentloaded", timeout=60000)
        return self._cleared(20)

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


def polite_fetch(br: Browser, url: str, sleep: float) -> tuple[str | None, str]:
    """(html, status), as in pipeline/tapology.py: two backed-off retries for a soft
    failure, one quiet re-clear per browser check (MAX_RECLEARS a run), and a stop
    at once on 429/503."""
    status = None
    for attempt in range(len(BACKOFF) + 1):
        res = br.fetch(url)
        time.sleep(sleep)
        if res.get("html"):
            return res["html"], "ok"
        status = res.get("status")
        if status == "CHALLENGE":
            if br.reclears >= MAX_RECLEARS:
                raise Blocked(f"browser check at {url}")
            br.reclears += 1
            log(f"  browser check on {url.rsplit('/', 1)[-1]} -> quiet {RECLEAR_WAIT}s, re-clear "
                f"{br.reclears}/{MAX_RECLEARS}")
            if not br.reclear():
                raise Blocked(f"browser check at {url} did not clear")
            continue
        if status in (404, 410):
            return None, str(status)
        if status in RATE_LIMITED:
            raise Stop(f"rate limited ({status}) at {url}")
        if attempt < len(BACKOFF):
            log(f"  soft {status} on {url.rsplit('/', 1)[-1]} -> wait {BACKOFF[attempt]}s")
            time.sleep(BACKOFF[attempt])
    return None, f"gave_up_{status}"


def discover(br: Browser, page1: str, accounted: set[str], since: str, today: str, sleep: float
             ) -> list[dict]:
    """Listed events dated after `since` and by today that are neither archived nor
    queued, newest first. Reads ?page=all only when page 1 may not reach back far
    enough: none of its past events is accounted for, and its oldest is after `since`."""
    listed = [e for e in parse_events_list(page1) if e["event_date"]]
    past = [e for e in listed if e["event_date"] <= today]
    if past and not any(e["ufcstats_event_id"] in accounted for e in past) and past[-1]["event_date"] > since:
        html, status = polite_fetch(br, f"{LISTING}?page=all", sleep)
        if html is None:
            raise Stop(f"full events list: {status}")
        listed = [e for e in parse_events_list(html) if e["event_date"]]
        log(f"  full events list: {len(listed)} events")
    new = [e for e in listed if since < e["event_date"] <= today and e["ufcstats_event_id"] not in accounted]
    log(f"  events list: {len(listed)} events, {len(new)} completed since {since} not yet archived or queued")
    return new


def fight_rows(fp: dict, key: str, fetch_date: str) -> dict[str, list]:
    out = {"ufcstats_fights": [fp["fight"]], "ufcstats_fight_totals": fp["totals"],
           "ufcstats_fight_rounds": fp["rounds"], "ufcstats_sig_strikes_by_target": fp["sig_by_target"],
           "ufcstats_sig_strikes_by_position": fp["sig_by_position"]}
    for rows in out.values():
        for r in rows:
            r["_raw_html_key"], r["fetch_date"] = key, fetch_date
    return out


def qa(ev: dict, fights: list[dict]) -> list[dict]:
    """Fatal codes are in FATAL_QA; the rest are warnings worth a look."""
    out = []
    def flag(code, slug, detail=""):
        out.append({"slug": slug, "code": code, "detail": detail})
    if len(fights) != len(ev["fights"]):
        flag("card_incomplete", ev["ufcstats_event_id"], f"{len(fights)} of {len(ev['fights'])} fight pages")
    for fp in fights:
        f, fid = fp["fight"], fp["fight"]["ufcstats_fight_id"]
        if f["ufcstats_event_id"] != ev["ufcstats_event_id"]:
            flag("fight_on_other_event", fid, str(f["ufcstats_event_id"]))
        if not (f["red_fighter_id"] and f["blue_fighter_id"]):
            flag("fight_not_two_fighters", fid)
        if len(fp["totals"]) != 2:
            flag("no_totals", fid, f"{len(fp['totals'])} totals rows")
        if f["winner_fighter_id"] is None:
            flag("no_winner", fid, str(f["results"]))
        by_round = Counter()
        for r in fp["rounds"]:
            by_round[r["ufcstats_fighter_id"]] += r["sig_str_land"] or 0
        for t in fp["totals"]:
            if fp["rounds"] and by_round[t["ufcstats_fighter_id"]] != (t["sig_str_land"] or 0):
                flag("rounds_disagree_with_totals", fid, f"{t['ufcstats_fighter_id']}: "
                     f"{by_round[t['ufcstats_fighter_id']]} by round vs {t['sig_str_land']} total")
    return out


# no_totals is a warning: ~3% of fights (mostly before ~2000) have no stats on UFC Stats.
FATAL_QA = {"card_incomplete", "fight_on_other_event", "fight_not_two_fighters"}


def crawl(limit: int, sleep: float, budget: Budget, env: str, run_dir: Path, known: set[str],
          state: dict, since: str, discover_events: bool = True, card_fighters: bool = True,
          claim_linked=None) -> dict:
    """Fetch, archive locally and parse until the work or the budget runs out:
    events new on the list (newest first), then events earlier runs deferred, then
    the fighter pages of every fighter on a kept card (and those deferred).

    A fight or fighter page that is fetched but does not parse is still archived
    (the archive is what extraction is fixed against) and reported as parse_failed.
    With discover_events=False only `state`'s queue and fighters are fetched and
    the saved queue is left alone (the leftovers come back as "leftover"); with
    card_fighters=False a kept card adds no fighter pages (a backfill shard fetches
    its own share of every fighter instead).

    Every event a fetched fighter page links to that nobody has is added to the
    events to fetch, if `claim_linked(event_id)` agrees: that is how events the
    completed list leaves out (DWCS, ...) are found. A daily run claims them all,
    for its next run; a backfill shard claims its share and fetches its fighters
    first, so it gets to them in the same run."""
    from playwright.sync_api import sync_playwright

    if claim_linked is None and discover_events:
        claim_linked = lambda e: True
    fetch_date = date.today().isoformat()
    settled_before = (date.today() - timedelta(days=SETTLE_DAYS)).isoformat()
    raw_dir = run_dir / "raw"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "meta.json").write_text(json.dumps({"fetch_date": fetch_date, "env": env}))
    rows = {t: [] for t in TABLES}
    pages: list[tuple[Path, str]] = []
    report, anomalies, stopped = Counter(), [], None

    queue = [q for q in state.get("queue", []) if q["id"] not in known]
    abandoned = set(state.get("abandoned", []))
    fighters: list[str] = list(dict.fromkeys(state.get("fighters", [])))
    worklist: list[dict] = list(queue)
    done: set[str] = set()
    retry: list[dict] = []
    fighters_done: set[str] = set()
    claimed = known | abandoned | {q["id"] for q in queue}

    def defer(item: dict, why: str) -> None:
        tries = item.get("tries", 0) + 1
        if tries >= MAX_TRIES:
            abandoned.add(item["id"])
            report["events_dropped_after_retries"] += 1
            log(f"  dropping {item['id']} after {tries} tries ({why})")
        else:
            retry.append({**item, "tries": tries})

    def archive(entity: str, ident: str, html: str) -> str:
        key = raw_html_key("ufcstats", entity, fetch_date, ident, env)
        local = raw_dir / entity / f"{ident}.html.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(local, "wb", compresslevel=6) as f:     # archive BEFORE loading
            f.write(html.encode("utf-8"))
        pages.append((local, key))
        return key

    def fetch_events(br: Browser) -> None:
        nonlocal fighters
        for item in worklist:                       # fetch_fighters may still be appending
            if limit and report["events_kept"] >= limit:
                break
            eid = item["id"]
            if eid in done or item.get("after", "") > fetch_date:  # not held yet; stays queued
                continue
            html, status = polite_fetch(br, f"{BASE}/event-details/{eid}", sleep)
            if html is None:
                report[f"event_miss_{status}"] += 1
                done.add(eid)
                if status in ("404", "410"):
                    abandoned.add(eid)                 # gone from UFC Stats for good
                else:
                    defer(item, status)
                continue
            try:
                ev = parse_event(html, eid)
            except Exception as ex:                    # a parser bug must not stop the crawl
                done.add(eid)
                report["event_parse_error"] += 1
                anomalies.append({"slug": eid, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
                defer(item, "parse_failed")
                continue
            when = ev["event_date"] or ""
            # Results are in, or the event is long enough past that they never will
            # be (a bout UFC Stats left without a flag): keep it, flagged.
            if not ev["completed"] and not (ev["fights"] and when and when < settled_before):
                done.add(eid)
                report["event_not_completed"] += 1
                if when >= fetch_date:                  # today or later: no try spent yet
                    retry.append({**item, "after": when})
                else:
                    defer(item, "not_completed")
                continue
            if not ev["completed"]:
                anomalies.append({"slug": eid, "code": "results_missing", "detail": f"dated {when}"})

            fight_pages, failed = [], None
            for f in ev["fights"]:
                fhtml, fstatus = polite_fetch(br, f"{BASE}/fight-details/{f['ufcstats_fight_id']}", sleep)
                if fhtml is None:
                    failed = f"fight {f['ufcstats_fight_id']}: {fstatus}"
                    break
                fight_pages.append((f["ufcstats_fight_id"], fhtml))
            done.add(eid)
            if failed:                                  # all or nothing per event
                report["event_fight_page_failed"] += 1
                defer(item, failed)
                continue

            key = archive("event", eid, html)
            ev_row = {k: v for k, v in ev.items() if k != "completed"}
            ev_row["_raw_html_key"], ev_row["fetch_date"] = key, fetch_date
            rows["ufcstats_events"].append(ev_row)
            fights = []
            for fid, fhtml in fight_pages:
                fkey = archive("fight", fid, fhtml)
                try:
                    fp = parse_fight(fhtml, fid)
                except Exception as ex:                # archived all the same; fix and replay
                    anomalies.append({"slug": fid, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
                    continue
                fights.append(fp)
                for t, rs in fight_rows(fp, fkey, fetch_date).items():
                    rows[t] += rs
            anomalies.extend(qa(ev, fights))
            if card_fighters:
                fighters += [x for f in ev["fights"] for x in f["fighter_ids"] if x]
            report["events_kept"] += 1
            report["fight_pages_kept"] += len(fights)
            log(f"  event {report['events_kept']:>4} {ev['event_name']} ({ev['event_date']}, {len(fights)} fights)")

    def fetch_fighters(br: Browser) -> None:
        for fid in list(dict.fromkeys(fighters)):
            if fid in fighters_done:
                continue
            html, status = polite_fetch(br, f"{BASE}/fighter-details/{fid}", sleep)
            fighters_done.add(fid)
            if html is None:
                report[f"fighter_miss_{status}"] += 1
                continue
            key = archive("fighter", fid, html)
            try:
                row = parse_fighter(html, fid)
            except Exception as ex:
                anomalies.append({"slug": fid, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
                continue
            row["_raw_html_key"], row["fetch_date"] = key, fetch_date
            rows["ufcstats_fighters"].append(row)
            report["fighter_pages_kept"] += 1
            if report["fighter_pages_kept"] % 250 == 0:
                log(f"  {report['fighter_pages_kept']} fighter pages")
            for f in row["fights"] if claim_linked else ():
                e = f["ufcstats_event_id"]
                if e and e not in claimed and claim_linked(e):
                    claimed.add(e)
                    worklist.append({"id": e, "tries": 0})
                    report["linked_events_claimed"] += 1

    with sync_playwright() as pw:
        br = Browser(pw, budget)
        try:
            page1 = br.open()
            if page1 is None:
                raise Blocked("the browser check did not clear on the events list")
            log("browser ready, UFC Stats' browser check cleared")
            if discover_events:
                new = discover(br, page1, claimed, since, fetch_date, sleep)
                worklist[:0] = [{"id": e["ufcstats_event_id"], "date": e["event_date"], "tries": 0} for e in new]
                claimed |= {e["ufcstats_event_id"] for e in new}
                log(f"{len(new)} events new on the list, {len(queue)} queued by earlier runs, "
                    f"{len(fighters)} fighter pages deferred")
            if not discover_events:                 # a backfill shard
                fetch_fighters(br)
                log(f"fighters done; {report['linked_events_claimed']} more events linked from their pages")
            fetch_events(br)
            fetch_fighters(br)
        except Stop as e:
            stopped = f"stopped: {e}"
            log(stopped)
        finally:
            br.close()

    queue_next = [q for q in worklist if q["id"] not in done] + retry
    fighters_next = [f for f in dict.fromkeys(fighters) if f not in fighters_done]
    report.update(requests=budget.spent, queued_for_next_run=len(queue_next), fighters_for_next_run=len(fighters_next))
    log(f"{budget.spent} requests spent; {len(queue_next)} events and {len(fighters_next)} fighter pages "
        f"queued for the next run")
    state_next = {"queue": queue_next, "abandoned": sorted(abandoned), "fighters": fighters_next}
    return {"fetch_date": fetch_date, "rows": rows, "pages": pages, "report": dict(report), "anomalies": anomalies,
            "stopped": stopped, "state": state_next if discover_events else None,
            "leftover": queue_next, "fighters_left": fighters_next, "abandoned": sorted(abandoned)}


def rebuild(run_dir: Path, env: str) -> dict:
    """Re-parse a run's archived pages with no network: a parser bug is fixed and
    replayed, never re-crawled."""
    fetch_date = json.loads((run_dir / "meta.json").read_text())["fetch_date"]
    rows = {t: [] for t in TABLES}
    pages, anomalies, report = [], [], Counter()

    def read(entity: str):
        for local in sorted((run_dir / "raw" / entity).glob("*.html.gz")):
            ident = local.name[: -len(".html.gz")]
            key = raw_html_key("ufcstats", entity, fetch_date, ident, env)
            pages.append((local, key))
            yield ident, gzip.decompress(local.read_bytes()).decode("utf-8"), key

    fights = {}
    for fid, html, key in read("fight"):
        try:
            fights[fid] = (parse_fight(html, fid), key)
        except Exception as ex:
            anomalies.append({"slug": fid, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
    for eid, html, key in read("event"):
        ev = parse_event(html, eid)
        ev_row = {k: v for k, v in ev.items() if k != "completed"}
        ev_row["_raw_html_key"], ev_row["fetch_date"] = key, fetch_date
        rows["ufcstats_events"].append(ev_row)
        mine = [fights[f["ufcstats_fight_id"]] for f in ev["fights"] if f["ufcstats_fight_id"] in fights]
        for fp, fkey in mine:
            for t, rs in fight_rows(fp, fkey, fetch_date).items():
                rows[t] += rs
        anomalies += qa(ev, [fp for fp, _ in mine])
        report["events_kept"] += 1
        report["fight_pages_kept"] += len(mine)
    for fid, html, key in read("fighter"):
        try:
            row = parse_fighter(html, fid)
        except Exception as ex:
            anomalies.append({"slug": fid, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
            continue
        row["_raw_html_key"], row["fetch_date"] = key, fetch_date
        rows["ufcstats_fighters"].append(row)
        report["fighter_pages_kept"] += 1
    return {"fetch_date": fetch_date, "rows": rows, "pages": pages, "report": dict(report),
            "anomalies": anomalies, "stopped": None, "state": None}


def verify(result: dict, staged: dict, run_dir: Path, env: str, lake: Lake | None, known: set[str]) -> dict:
    """The raw archive, parse quality and the Parquet, before anything loads."""
    fails, warns = [], []
    rows, pages = result["rows"], result["pages"]
    raw_keys = {k for _, k in pages}
    with ThreadPoolExecutor(LAKE_THREADS) as pool:
        in_bucket = dict(pool.map(lambda p: (p[1], lake.size(p[1])), pages)) if lake else {}
    for local, key in pages:
        try:
            gzip.decompress(local.read_bytes())
        except OSError:
            fails.append(f"corrupt gzip: {local.name}")
        if lake and in_bucket[key] != local.stat().st_size:
            fails.append(f"raw page missing or wrong size in bucket: {key}")
    for e in rows["ufcstats_events"]:
        if e["ufcstats_event_id"] in known:
            fails.append(f"event already archived was fetched again: {e['ufcstats_event_id']}")
    for a in result["anomalies"]:
        (fails if a["code"] in FATAL_QA | {"parse_failed"} else warns).append(
            f"qa {a['code']}: {a['slug']} {a['detail'][:80]}")
    for table, recs in rows.items():
        n = staged.get(table, (0, []))[0]
        if n != len(recs):
            fails.append(f"{table}: {len(recs)} rows parsed but {n} in Parquet")
        if {r["_raw_html_key"] for r in recs} - raw_keys:
            fails.append(f"{table}: rows point at an unarchived page")
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
    """Event ids already archived. DEV also skips its own earlier test crawls."""
    zones = ("",) if env == "prod" else ("", "dev/")
    return lake.archived_slugs("ufcstats", "event", zones)


def known_fighters(lake: Lake, env: str) -> set[str]:
    zones = ("",) if env == "prod" else ("", "dev/")
    return lake.archived_slugs("ufcstats", "fighter", zones)


def backfill_key(env: str, name: str) -> str:
    return f"{zone(env)}state/ufcstats/backfill/{name}.json"


def linked_events(env: str) -> set[str]:
    """Events that fighter pages in LANDING link to: the ones the completed-events
    list leaves out (DWCS, PRIDE, UFC 1, ...). Empty before the first load."""
    try:
        cur = snowflake_cursor(env)
        cur.execute(f"""select distinct f.value:ufcstats_event_id::string
                        from FIGHTGRADE_{env.upper()}.LANDING.LND_UFCSTATS_FIGHTERS,
                             lateral flatten(input => payload:fights) f
                        where f.value:ufcstats_event_id::string is not null""")
        return {r[0] for r in cur.fetchall()}
    except Exception as ex:                       # no table yet: the first plan
        log(f"  no fighter-linked events from LANDING ({type(ex).__name__})")
        return set()


def plan_backfill(lake: Lake, env: str, sleep: float, budget: Budget, known: set[str]) -> dict:
    """All of UFC Stats as one fixed list that shards split: every listed event dated
    by today, every event the fighter pages in LANDING link to, and every fighter on
    the 26 A-Z fighter listings. 27 requests after the first page.

    The list leaves ~475 events out, so a shard also claims the events its own
    fighter pages link to (see backfill_shard). Planning again after the LANDING
    load is the completeness check: `to_fetch` is what a second round would get."""
    from playwright.sync_api import sync_playwright

    today = date.today().isoformat()
    with sync_playwright() as pw:
        br = Browser(pw, budget)
        try:
            if br.open() is None:
                raise Blocked("the browser check did not clear on the events list")
            html, status = polite_fetch(br, f"{LISTING}?page=all", sleep)
            if html is None:
                raise RuntimeError(f"full events list: {status}; no plan written")
            events = [e["ufcstats_event_id"] for e in parse_events_list(html)
                      if e["event_date"] and e["event_date"] <= today]
            fighters: list[str] = []
            for c in "abcdefghijklmnopqrstuvwxyz":
                html, status = polite_fetch(br, FIGHTER_LISTING.format(c), sleep)
                if html is None:
                    raise RuntimeError(f"fighter listing {c!r}: {status}; no plan written")
                fighters += parse_fighters_list(html)
        finally:
            br.close()
    listed = len(events)
    events = list(dict.fromkeys(events + sorted(linked_events(env))))
    plan = {"created": today, "events": events, "fighters": list(dict.fromkeys(fighters))}
    lake.write_json(backfill_key(env, "plan"), plan)
    to_fetch = len([e for e in events if e not in known])
    log(f"backfill plan: {len(events)} events ({listed} listed, {len(events) - listed} linked from fighter "
        f"pages; {to_fetch} not archived) and {len(plan['fighters'])} fighters, in {backfill_key(env, 'plan')}")
    return {"fetch_date": today, "rows": {t: [] for t in TABLES}, "pages": [], "anomalies": [], "stopped": None,
            "report": {"planned_events": len(events), "listed_events": listed, "events_to_fetch": to_fetch,
                       "planned_fighters": len(plan["fighters"]), "requests": budget.spent}, "state": None}


def backfill_shard(lake: Lake, env: str, run_dir: Path, sleep: float, budget: Budget, known: set[str],
                   shard: int, shards: int) -> dict:
    """Shard `shard` of `shards` of the saved plan: every shards-th fighter first,
    then every shards-th event (with its fight pages) plus the events those fighter
    pages link to that are not in the plan and whose id falls to this shard, minus
    what is archived by now. A shard cut short is simply run again; its unfinished
    linked events come back from its leftover file."""
    plan = lake.read_json(backfill_key(env, "plan"), None)
    if plan is None:
        raise RuntimeError("no backfill plan: run with backfill_plan first")
    planned = set(plan["events"])
    mine = [e for i, e in enumerate(plan["events"]) if i % shards == shard]
    before = lake.read_json(backfill_key(env, f"shard-{shard}"), {})
    mine += [q["id"] for q in before.get("leftover", []) if q["id"] not in planned]
    todo = [e for e in dict.fromkeys(mine) if e not in known]
    have = known_fighters(lake, env)
    my_fighters = [f for i, f in enumerate(plan["fighters"]) if i % shards == shard]
    todo_fighters = [f for f in my_fighters if f not in have]
    log(f"backfill shard {shard}/{shards}: {len(mine)} events ({len(todo)} not archived yet), "
        f"{len(my_fighters)} fighters ({len(todo_fighters)} not archived yet)")
    return crawl(0, sleep, budget, env, run_dir, known,
                 {"queue": [{"id": e, "tries": 0} for e in todo], "fighters": todo_fighters}, "",
                 discover_events=False, card_fighters=False,
                 claim_linked=lambda e: e not in planned and int(e, 16) % shards == shard)


@contextmanager
def ip_budget(lake: Lake, max_requests: int, minutes: float = MAX_FETCH_MINUTES, daily: int = DAILY_BUDGET):
    """Today's remaining UFC Stats requests on this egress IP, with a lock so two runs
    never share an IP. Same rules as pipeline.tapology.ip_budget, its own ledger."""
    ip, today = egress_ip(), date.today().isoformat()
    lock = f"{LOCK_PREFIX}{ip}.json"
    held = lake.create(lock, {"since": time.time()})
    if not held and (lake.age_seconds(lock) or 0) > LOCK_STALE_HOURS * 3600:
        lake.delete(lock)
        held = lake.create(lock, {"since": time.time()})
    spent = lake.read_json(BUDGET_KEY, {}).get(ip, {}).get(today, 0)
    budget = Budget(max(min(max_requests, daily - spent), 0) if held else 0, minutes)
    log(f"egress IP {ip}: {spent}/{daily} UFC Stats requests spent today, this run may spend {budget.limit}"
        + ("" if held else " (another run holds this IP)"))
    try:
        yield budget
    finally:
        if held:
            ledger = lake.read_json(BUDGET_KEY, {})
            week_ago = (date.today() - timedelta(days=7)).isoformat()
            days = {d: n for d, n in ledger.get(ip, {}).items() if d > week_ago}
            days[today] = days.get(today, 0) + budget.spent
            ledger[ip] = days
            lake.write_json(BUDGET_KEY, ledger)
            lake.delete(lock)


def upload(lake: Lake, result: dict, staged: dict, run_dir: Path, env: str) -> tuple[int, int]:
    """Raw pages first, LAKE_THREADS at a time, then the Parquet that points at them."""
    with ThreadPoolExecutor(LAKE_THREADS) as pool:
        list(pool.map(lambda p: lake.upload_file(p[0], p[1], "application/gzip"), result["pages"]))
    n_files = 0
    for _, files in staged.values():
        for f in files:
            lake.upload_file(f, parsed_key(f, run_dir, env), "application/octet-stream")
            n_files += 1
    return len(result["pages"]), n_files


def save_shard_leftover(lake: Lake, env: str, shard: int, run_id: str, result: dict) -> None:
    """What a shard could not finish, for a rerun or the daily queue to pick up."""
    lake.write_json(backfill_key(env, f"shard-{shard}"),
                    {"run_id": run_id, "leftover": result["leftover"], "fighters_left": result["fighters_left"],
                     "abandoned": result["abandoned"], "anomalies": result["anomalies"][:500]})


def save_queue(lake: Lake, env: str, result: dict) -> None:
    """After the run's pages are in the lake, so nothing leaves the queue unarchived."""
    if result.get("state") is not None:
        lake.write_json(queue_key(env), result["state"])


def landing_counts(run_id: str, staged: dict, env: str) -> dict:
    """Rows of this run's files that LANDING now holds, per table."""
    cur, out = snowflake_cursor(env), {}
    for table, (n, _) in staged.items():
        cur.execute(f"select count(*) from FIGHTGRADE_{env.upper()}.LANDING.LND_{table.upper()} "
                    f"where _object_key like %s", (f"%part-{run_id}-%",))
        out[table] = {"parquet": n, "landing": cur.fetchone()[0]}
    return out


DBT_SELECT = "path:models/landing/ufcstats"     # only this source's tables: see dbt/models/landing/


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
                    help=f"requests this run may spend, within today's {DAILY_BUDGET} for this IP")
    ap.add_argument("--max-minutes", type=float, default=MAX_FETCH_MINUTES,
                    help=f"stop fetching after this many minutes (default {MAX_FETCH_MINUTES})")
    ap.add_argument("--since", default=EXPORT_LAST_EVENT,
                    help=f"only events dated after this (default {EXPORT_LAST_EVENT}, the export's last event)")
    ap.add_argument("--dry-run", action="store_true", help="crawl and stage locally; upload and load nothing")
    ap.add_argument("--from-run", metavar="RUN_ID", help="re-parse that run's archived pages instead of crawling")
    ap.add_argument("--backfill-plan", action="store_true", help="write the all-of-UFC-Stats plan; fetch no events")
    ap.add_argument("--backfill-shard", type=int, metavar="K", help="with --backfill-shards: fetch this shard of the plan")
    ap.add_argument("--backfill-shards", type=int, default=0, metavar="N")
    ap.add_argument("--daily-budget", type=int, default=DAILY_BUDGET, help="the per-IP daily cap; a backfill may raise it")
    args = ap.parse_args()
    if args.sleep < MIN_SLEEP:
        ap.error(f"--sleep below {MIN_SLEEP} is faster than anything measured (see DEFAULT_SLEEP)")

    env = environment()
    run_id = args.from_run or new_run_id()
    run_dir = RUNS / run_id
    log(f"run {run_id} env={env} zone='{zone(env)}' -> {run_dir.relative_to(ROOT)}")

    lake = Lake(env)
    known = known_events(lake, env)
    log(f"{len(known):,} UFC Stats events already archived")
    if args.from_run:
        result = rebuild(run_dir, env)
        known -= {e["ufcstats_event_id"] for e in result["rows"]["ufcstats_events"]}
    else:
        with ip_budget(lake, args.max_requests, args.max_minutes, args.daily_budget) as budget:
            if args.backfill_plan:
                result = plan_backfill(lake, env, args.sleep, budget, known)
            elif args.backfill_shards:
                result = backfill_shard(lake, env, run_dir, args.sleep, budget, known,
                                        args.backfill_shard, args.backfill_shards)
            else:
                state = lake.read_json(queue_key(env), {})
                result = crawl(args.limit, args.sleep, budget, env, run_dir, known, state, args.since)
    shutil.rmtree(run_dir / "tables", ignore_errors=True)
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = stage(result["rows"], run_dir, run_id)

    backfill = args.backfill_plan or bool(args.backfill_shards)
    if not args.dry_run:
        n_pages, n_files = upload(lake, result, staged, run_dir, env)
        save_queue(lake, env, result)
        if args.backfill_shards:
            save_shard_leftover(lake, env, args.backfill_shard, run_id, result)
        log(f"uploaded {n_pages} pages and {n_files} Parquet files")

    checks = verify(result, staged, run_dir, env, None if args.dry_run else lake, known)
    report = {"run_id": run_id, "env": env, "fetch_date": result["fetch_date"], "crawl": result["report"],
              "rows": {t: len(r) for t, r in result["rows"].items()}, **checks}
    # A backfill loads LANDING once, after every shard (see orchestration/ufcstats.py).
    if not args.dry_run and not backfill and not checks["fails"] and staged:
        report["dbt_success"] = load_landing_cli(env)
        report["landing"] = landing_counts(run_id, staged, env)
        for t, c in report["landing"].items():
            if c["parquet"] != c["landing"]:
                report["fails"].append(f"LANDING {t}: {c['landing']} rows loaded, {c['parquet']} expected")
        if not report["dbt_success"]:
            report["fails"].append(f"dbt build --select {DBT_SELECT} failed")

    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, indent=1))
    log("report:\n" + json.dumps(report, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
