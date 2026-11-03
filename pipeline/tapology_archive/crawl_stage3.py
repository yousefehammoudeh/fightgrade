#!/usr/bin/env python3
"""Stage-3 crawler: event pages -> raw HTML archive + parsed records + QA report.

Method is stage 2's, unchanged (see docs/scraping-method.md): headed real Chrome holds
the Cloudflare clearance, all fetching happens as same-origin fetch() inside that page.
What changed is what we keep: the raw HTML is archived gzipped BEFORE parsing, so a
parser fix later re-applies to every page in minutes instead of costing another crawl.

Outputs (all append-only, resumable):
  data/html/<aa>/<bb>/<slug>.html.gz   raw page, gzip -6
  data/event_details.jsonl             one row per event (no bouts)
  data/bouts.jsonl                     one row per bout
  data/event_fighters.jsonl            event <-> fighter link rows
  logs/anomalies.jsonl                 QA flags, the thing to read after a run
  logs/labels.json                     label/vocabulary registry (drift detector)
  logs/events_done.txt                 checkpoint
  logs/status3.json, logs/crawl3.log

Usage:
  python3 -m pipeline.tapology_archive.crawl_stage3 [--limit N] [--sleep SECS] [--newest-first]
"""
import argparse, gzip, hashlib, json, os, re, shutil, signal, subprocess, sys, tempfile, time
from collections import Counter
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from pipeline.parsers.tapology import parse_event             # noqa: E402
from playwright.sync_api import sync_playwright                # noqa: E402
from playwright._impl._errors import TargetClosedError, Error as PWError  # noqa: E402

STATE   = ROOT / "logs"
STATE_DIR = STATE
EVENTS  = ROOT / "data" / "events.jsonl"
HTMLDIR = ROOT / "data" / "html"
DETAILS = ROOT / "data" / "event_details.jsonl"
BOUTS   = ROOT / "data" / "bouts.jsonl"
EFIGHT  = ROOT / "data" / "event_fighters.jsonl"
ANOM    = STATE / "anomalies.jsonl"
LABELS  = STATE / "labels.json"
DONE    = STATE / "events_done.txt"
STATUS  = STATE / "status3.json"
LOG     = STATE / "crawl3.log"

EXPRESSVPNCTL = "/Applications/ExpressVPN.app/Contents/MacOS/expressvpnctl"
VPN_CANDIDATES = STATE_DIR / "vpn_candidates.txt"   # every region the CLI offers
VPN_POOL = STATE_DIR / "vpn_pool.json"              # active pool + health, persisted
POOL_SIZE = 43
RETIRE_FAILS = 2        # never cleared CF this many times -> retire
RETIRE_STREAK = 3       # or failed this many times in a row after previously working

def load_vpn_pool():
    """Active regions + per-region health, persisted across restarts."""
    cands = [l.strip() for l in VPN_CANDIDATES.read_text().splitlines() if l.strip()] \
        if VPN_CANDIDATES.exists() else []
    st = {"active": [], "retired": [], "health": {}}
    if VPN_POOL.exists():
        try:
            st.update(json.loads(VPN_POOL.read_text()))
        except Exception:
            pass
    if not st["active"]:
        seen, act = set(st["retired"]), []
        by = {}
        for c in cands:                      # spread across countries, not 30 US cities
            by.setdefault(c.split("-")[0], []).append(c)
        i = 0
        while len(act) < POOL_SIZE and any(i < len(v) for v in by.values()):
            for k in sorted(by):
                if i < len(by[k]) and by[k][i] not in seen and len(act) < POOL_SIZE:
                    act.append(by[k][i])
            i += 1
        st["active"] = act
    st["candidates"] = cands
    return st

def save_vpn_pool(st):
    atomic = {k: st[k] for k in ("active", "retired", "health")}
    tmp = VPN_POOL.with_suffix(".tmp")
    tmp.write_text(json.dumps(atomic, indent=1))
    os.replace(tmp, VPN_POOL)


JS = r"""
async (u) => {
  try {
    // A request that never settles (a VPN exit swap mid-fetch, 2026-10-29) hung the run for
    // hours: page.evaluate has no timeout of its own. The abort covers the body read too.
    const r = await fetch(u, {credentials:"include", signal: AbortSignal.timeout(60000)});
    const b = await r.text();
    if (/Just a moment/i.test(b)) return {status:"CHALLENGE", html:null};
    // Tapology serves a tiny "Temporarily Unavailable" page under load. It arrives as a
    // normal response, so without this check it gets archived, parses to nothing, and
    // the event is marked done -- silently lost.
    if (/Temporarily Unavailable/i.test(b) || b.length < 5000)
      return {status:"UNAVAILABLE", html:null};
    return {status:r.status, html:b};
  } catch (e) { return {status:"FAIL", html:null}; }
}
"""

MON = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")

def event_date(raw, this_year):
    """Row text carries a full date for past years and a YEARLESS one for the current
    year ("Saturday, August 15, 5:30 PM ET"). Returns (date, precision)."""
    m = re.search(r"(January|February|March|April|May|June|July|August|September|"
                  r"October|November|December) (\d{1,2}), (\d{4})", raw or "")
    if m:
        try:
            return date(int(m.group(3)), MON[m.group(1)], int(m.group(2))), "full"
        except ValueError:
            pass
    m = re.search(r"(January|February|March|April|May|June|July|August|September|"
                  r"October|November|December) (\d{1,2}),", raw or "")
    if m:
        try:
            return date(this_year, MON[m.group(1)], int(m.group(2))), "assumed_year"
        except ValueError:
            pass
    return date(9999, 12, 31), "unknown"

def html_path(slug):
    h = hashlib.sha1(slug.encode()).hexdigest()
    return HTMLDIR / h[:2] / h[2:4] / (slug + ".html.gz")

def load_done():
    if not DONE.exists():
        return set()
    return {l.strip() for l in DONE.read_text().splitlines() if l.strip()}

def build_worklist(newest_first=False):
    this_year = date.today().year
    rows = []
    for line in EVENTS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue                       # tolerate a torn line rather than crash-loop
        d, prec = event_date(e.get("raw_row_text"), this_year)
        rows.append({"date": d.isoformat(), "date_precision": prec,
                     "event_id": e.get("event_id"), "slug": e.get("event_slug"),
                     "promotion_id": e.get("promotion_id")})
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: (r["date"], r["event_id"] or 0),
                    reverse=newest_first):
        if r["slug"] and r["slug"] not in seen:
            seen.add(r["slug"]); uniq.append(r)
    return uniq

# ------------------------------------------------------------------ QA ------
def qa(ev, row):
    """Invariants. Anything here lands in anomalies.jsonl for a human pass -- the point
    is that a silently-empty field is reported, not discovered months later."""
    out = []
    def flag(code, detail=""):
        out.append({"slug": row["slug"], "code": code, "detail": str(detail)[:200]})
    if not ev.get("parse_ok"):
        flag("parse_failed", "no production block and no bouts")
        return out                          # everything else would be noise
    if not ev.get("bouts"):
        # Tapology states "No fight card available for this event" on records it simply
        # has no card for -- that is known-empty, not a scrape failure.
        if ev.get("card_note") or ev.get("status") == "Cancelled":
            flag("zero_bouts_declared", ev.get("card_note") or ev.get("status"))
        else:
            flag("zero_bouts_unexplained")
    if not ev["production"].get("date_time"):
        flag("no_date")
    if not ev["promotion"].get("promotion_slug"):
        flag("no_promotion")
    if not ev.get("bout_counts_agree"):
        flag("counts_disagree", f"card={ev['bout_counts']} declared={ev['bout_counts_declared']}")
    if ev.get("status") == "Completed":
        for b in ev["bouts"]:
            if any(f["result"] for f in b["fighters"]):
                continue
            if (b["method"] or "").strip().lower() == "result unknown":
                continue          # the page says so explicitly; nothing was missed
            flag("completed_bout_no_result", b["bout_slug"] or b["bout_id"])
            break
    for b in ev["bouts"]:
        if len(b["fighters"]) != 2:
            flag("bout_not_two_fighters", b["bout_id"])
        if any(not f["fighter_slug"] for f in b["fighters"]):
            flag("fighter_without_slug", b["bout_id"])
        if not b["sport"]:
            flag("bout_without_sport", b["bout_id"])
    return out

def collect_labels(ev, reg):
    reg["production"].update(ev["production_raw"].keys())
    reg["status"].update([ev.get("status") or "None"])
    reg["sport_label"].update([ev.get("sport_label") or "None"])
    for b in ev["bouts"]:
        reg["comparison"].update(v["label"] for v in b["comparison"].values())
        if b["sport"]:
            reg["bout_sport"].update([b["sport"]])
        if b["method"]:
            reg["method"].update([b["method"].split(",")[0].strip()])
        if b.get("title_kind"):
            reg["title_kind"].update([b["title_kind"]])

PAGE_DATE_US = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")            # "Tuesday 10.01.2013"
PAGE_DATE_LONG = re.compile(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})")     # "October 1, 2013"

def page_date(ev):
    """The event page always states its own date. Sitemap-discovered events have no date
    in the index, so recover it from the page rather than leaving the 9999 sentinel."""
    dt = (ev.get("production") or {}).get("date_time") or ""
    m = PAGE_DATE_US.search(dt)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    m = PAGE_DATE_LONG.search(ev.get("date_header") or "")
    if m and m.group(1) in MON:
        try:
            return date(int(m.group(3)), MON[m.group(1)], int(m.group(2))).isoformat()
        except ValueError:
            pass
    return None

def flatten(ev, row):
    """Split one parsed event into the three output tables."""
    e = {k: v for k, v in ev.items() if k != "bouts"}
    if row.get("date_precision") == "unknown":
        pd_ = page_date(ev)
        if pd_:
            row = {**row, "date": pd_, "date_precision": "from_page"}
    e.update({"event_slug": row["slug"], "event_id": row["event_id"],
              "event_date_index": row["date"], "date_precision": row["date_precision"],
              "index_promotion_id": row["promotion_id"], "scraped_at": now()})
    bouts, links = [], []
    for b in ev["bouts"]:
        bouts.append({"event_slug": row["slug"], "event_id": row["event_id"], **b})
        for f in b["fighters"]:
            if f["fighter_slug"]:
                links.append({"event_slug": row["slug"], "bout_id": b["bout_id"],
                              "bout_slug": b["bout_slug"], "side": f["side"],
                              "fighter_slug": f["fighter_slug"], "name": f["name"],
                              "result": f["result"], "sport": b["sport"],
                              "title_kind": b.get("title_kind")})
    for c in ev.get("cancelled_bouts", []):
        for f in c["fighters"]:
            if f["fighter_slug"]:
                links.append({"event_slug": row["slug"], "bout_id": c["bout_id"],
                              "bout_slug": c["bout_slug"], "side": None,
                              "fighter_slug": f["fighter_slug"], "name": f["name"],
                              "result": c["status"], "sport": None, "title_kind": None})
    return e, bouts, links

def append_jsonl(path, records):
    if not records:
        return
    with path.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())     # data hits disk BEFORE the checkpoint names it done

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=2.0,
                    help="baseline seconds between events; rises automatically on 503s")
    ap.add_argument("--newest-first", action="store_true")
    ap.add_argument("--after", default=None, help="only events on/after YYYY-MM-DD")
    ap.add_argument("--before", default=None, help="only events before YYYY-MM-DD "
                    "(default: tomorrow -- scheduled events have no results to scrape)")
    ap.add_argument("--rotate-every", type=int, default=600,
                    help="events per IP before a proactive rotation")
    ap.add_argument("--include-future", action="store_true",
                    help="also crawl events dated after today")
    args = ap.parse_args()

    for p in (HTMLDIR, STATE):
        p.mkdir(parents=True, exist_ok=True)

    # Single-instance guard. Two crawlers sharing the append-only outputs produce
    # duplicate rows and race on the archive temp file, so refuse to start if another
    # one is alive. (A stale pid from a hard kill is ignored.)
    LOCK = STATE / "crawl_stage3.lock"
    if LOCK.exists():
        try:
            other = int(LOCK.read_text().strip())
            os.kill(other, 0)
            log(f"ABORT: crawler already running as pid {other}")
            sys.exit(3)
        except (ValueError, ProcessLookupError, PermissionError):
            log("stale lock file -> taking over")
    LOCK.write_text(str(os.getpid()))
    import atexit
    atexit.register(lambda: LOCK.unlink(missing_ok=True))

    work = build_worklist(args.newest_first)
    done = load_done()
    todo = [r for r in work if r["slug"] not in done]
    if args.after:
        todo = [r for r in todo if r["date"] >= args.after]
    # Scheduled events carry no results, so they are excluded by default: crawling them
    # now just banks an empty card that nothing would ever refresh. The stage-5 daily job
    # picks them up once they have actually happened.
    cutoff = args.before
    if cutoff is None and not args.include_future:
        cutoff = (date.today() + timedelta(days=1)).isoformat()
    if cutoff:
        # sitemap-discovered events have no date at all; they carry the 9999 sentinel and
        # must be kept (they sort last) rather than mistaken for scheduled future events
        skipped = sum(1 for r in todo
                      if r["date"] >= cutoff and r["date_precision"] != "unknown")
        todo = [r for r in todo
                if r["date"] < cutoff or r["date_precision"] == "unknown"]
        if skipped:
            log(f"  excluding {skipped} events dated on/after {cutoff} (not yet contested)")
    if args.limit:
        todo = todo[:args.limit]
    log(f"START stage-3: {len(work)} events indexed, {len(done)} done, "
        f"{len(todo)} queued this run (order={'newest' if args.newest_first else 'oldest'}-first)")
    if not todo:
        # completion flag: `remaining` never reaches 0 on its own because future-dated
        # events are deliberately never crawled, so the queue emptying is the real signal
        (STATE / "stage3.done").write_text(now())
        log("STAGE 3 COMPLETE (queue exhausted)")
        return
    if todo:
        log(f"  first: {todo[0]['date']}  {todo[0]['slug']}")
        log(f"  last : {todo[-1]['date']}  {todo[-1]['slug']}")

    reg = {k: Counter() for k in
           ("production", "comparison", "bout_sport", "method", "status",
            "sport_label", "title_kind")}
    if LABELS.exists():
        try:
            for k, v in json.loads(LABELS.read_text()).items():
                reg.setdefault(k, Counter()).update(v)
        except Exception:
            log("labels.json unreadable -> starting a fresh registry")

    stop = {"stop": False}
    def handler(sig, frame):
        stop["stop"] = True
        log(f"signal {sig} -> finishing current event, then exiting")
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)

    t0 = time.time()
    counts = Counter()
    done_buf = []
    SOFT_BACKOFF = [5, 15, 45]
    # Measured: an IP sustains ~1,300 events at 2.0s before Tapology starts 503ing, and a
    # rotation clears it instantly -- so the limit is per-IP. Rotating BEFORE the wall
    # costs the same number of rotations as waiting for it, minus ~9 min of stall each.
    ROTATE_EVERY = args.rotate_every
    soft = {"slug": None, "tries": 0, "streak": 0, "ok_run": 0, "since_rotate": 0}
    pace = {"s": args.sleep}          # adaptive: rises on soft failures, decays back

    with sync_playwright() as pw:
        B = {"ctx": None, "page": None, "profile": None}

        def close_browser():
            try:
                if B["ctx"]:
                    B["ctx"].close()
            except Exception:
                pass
            if B["profile"]:
                shutil.rmtree(B["profile"], ignore_errors=True)
            B["ctx"] = B["page"] = B["profile"] = None

        def new_browser():
            close_browser()
            B["profile"] = tempfile.mkdtemp(prefix="tap_chrome_")
            B["ctx"] = pw.chromium.launch_persistent_context(
                user_data_dir=B["profile"], channel="chrome", headless=False,
                viewport={"width": 1280, "height": 900}, locale="en-US")
            B["page"] = B["ctx"].pages[0] if B["ctx"].pages else B["ctx"].new_page()
            try:
                B["page"].goto("https://www.tapology.com/fightcenter",
                               wait_until="domcontentloaded", timeout=60000)
                # Successful clears land in 6-9s (Cloudflare's own JS challenge takes
                # 8-12s at most). A flagged region never clears at all, so waiting 50s
                # just burns a minute per bad region -- and ~25% of regions are bad.
                for _ in range(10):
                    if "Just a moment" not in B["page"].title():
                        log("browser ready, cf cleared")
                        return True
                    time.sleep(2)
            except (TargetClosedError, PWError):
                pass
            log("browser created but CF did NOT clear")
            return False

        def cooldown():
            """Silent escalating wait. Chrome fully closed -- poking keeps a block warm."""
            for k, secs in enumerate([300, 900, 1800, 3600, 7200]):
                if stop["stop"]:
                    return False
                log(f"  CF blocked -> SILENT COOLDOWN {secs}s (Chrome released) [{k+1}]")
                close_browser()
                time.sleep(secs)
                if new_browser():
                    log("  CF cleared -> resuming")
                    return True
            return False

        vpn = {"i": 0, "pool": load_vpn_pool()}
        save_vpn_pool(vpn["pool"])          # so the file always reflects the live pool
        log(f"vpn pool: {len(vpn['pool']['active'])} active, "
            f"{len(vpn['pool']['retired'])} retired ({vpn['pool']['retired'][:3]})")

        def _health(region):
            return vpn["pool"]["health"].setdefault(
                region, {"ok": 0, "fail": 0, "streak": 0})

        def _retire(region, why):
            """Drop a region that keeps failing and promote an unused candidate."""
            pool = vpn["pool"]
            if region in pool["active"]:
                pool["active"].remove(region)
            if region not in pool["retired"]:
                pool["retired"].append(region)
            spare = next((c for c in pool["candidates"]
                          if c not in pool["active"] and c not in pool["retired"]), None)
            if spare:
                pool["active"].append(spare)
            log(f"  RETIRED region {region} ({why}); replaced with {spare or 'NOTHING LEFT'} "
                f"[pool={len(pool['active'])}, retired={len(pool['retired'])}]")
            save_vpn_pool(pool)

        def rotate_vpn(reason=""):
            pool = vpn["pool"]
            # Least-recently-used order, persisted across restarts. A round-robin cursor
            # resets to 0 on every restart, which put us back on an IP that had been
            # rate-limited minutes earlier (185 events instead of ~530). LRU guarantees
            # each IP gets the maximum possible decay time before reuse.
            order = sorted(pool["active"],
                           key=lambda r: pool["health"].get(r, {}).get("last_used", 0))
            order = [r for r in order if r != vpn.get("current")] or order
            for region in order:
                if not pool["active"]:
                    log("  no active regions left -> cooldown")
                    return cooldown()
                h = _health(region)
                idle = time.time() - h.get("last_used", 0)
                log(f"  (region {region} idle {idle/60:.0f} min)" if h.get("last_used")
                    else f"  (region {region} never used)")
                log(f"  ROTATE VPN -> {region} ({reason})")
                close_browser()
                try:
                    subprocess.run([EXPRESSVPNCTL, "connect", region],
                                   capture_output=True, text=True, timeout=45)
                except Exception as e:
                    log(f"  vpn error: {type(e).__name__}")
                ok = False
                for _ in range(12):
                    try:
                        if "Connected" in subprocess.run(
                                [EXPRESSVPNCTL, "status"], capture_output=True,
                                text=True, timeout=15).stdout:
                            ok = True
                            break
                    except Exception:
                        pass
                    time.sleep(3)
                if ok and new_browser():
                    h["ok"] += 1; h["streak"] = 0
                    h["last_used"] = time.time()
                    vpn["current"] = region
                    save_vpn_pool(pool)
                    soft["since_rotate"] = 0
                    return True
                # connected but Cloudflare never cleared, or the connect itself failed
                h["fail"] += 1; h["streak"] += 1
                log(f"  region {region} failed (ok={h['ok']} fail={h['fail']} "
                    f"streak={h['streak']})")
                if (h["ok"] == 0 and h["fail"] >= RETIRE_FAILS) or h["streak"] >= RETIRE_STREAK:
                    _retire(region, f"ok={h['ok']} fail={h['fail']} streak={h['streak']}")
                else:
                    save_vpn_pool(pool)
            return cooldown()

        def flush(current=None):
            if done_buf:
                with DONE.open("a") as f:
                    f.write("".join(s + "\n" for s in done_buf))
                    f.flush(); os.fsync(f.fileno())
                done_buf.clear()
            LABELS.write_text(json.dumps({k: dict(v) for k, v in reg.items()},
                                         ensure_ascii=False, indent=1))
            el = time.time() - t0
            STATUS.write_text(json.dumps({
                "ts": now(), "indexed": len(work), "done": len(done),
                "remaining": len(work) - len(done), "this_run": counts["ok"],
                "anomalies": counts["anom"], "misses": counts["miss"],
                "rate_per_min": round(counts["ok"] / (el / 60), 2) if el > 60 else None,
                "current": current}, indent=1))

        # Start on a FRESH IP. A restart otherwise inherits whatever region the VPN was
        # left on -- which, right after a 503-triggered rotation, is a spent IP. That is
        # what produced two misleading short stints (185 and 222 events) that looked like
        # the pace being too aggressive when it was really a recycled address.
        if not rotate_vpn("startup-fresh-ip"):
            log("startup rotation failed entirely")

        i = 0
        while i < len(todo):
            if stop["stop"]:
                break
            if soft.get("since_rotate", 0) >= ROTATE_EVERY:
                log(f"  {soft['since_rotate']} events on this IP -> proactive rotation")
                rotate_vpn("proactive")
                soft["since_rotate"] = 0
            row = todo[i]
            url = "https://www.tapology.com/fightcenter/events/" + row["slug"]
            try:
                res = B["page"].evaluate(JS, url)
            except (TargetClosedError, PWError) as ex:
                log(f"  browser died at {row['slug']}: {type(ex).__name__} -> recover in-process")
                flush(f"recover at {row['slug']}")
                time.sleep(5)
                if not new_browser():
                    cooldown()
                continue                          # retry SAME event, never advance
            if not res.get("html"):
                st = res.get("status")
                if st == "CHALLENGE":
                    rotate_vpn("blocked")
                    continue                      # retry SAME event
                if st in (404, 410):              # genuinely gone: skip for good
                    counts["miss"] += 1
                    append_jsonl(ANOM, [{"slug": row["slug"], "code": "fetch_failed",
                                         "detail": str(st)}])
                    log(f"  MISS {row['slug']} status={st}")
                    i += 1
                    time.sleep(pace["s"])
                    continue
                # 503/5xx/network = SOFT rate limit. It is transient, so back off and
                # retry the SAME event -- never advance. Advancing here would race
                # through the worklist hammering an already-limited server, which is
                # what turns a soft limit into a hard IP block.
                if soft["slug"] != row["slug"]:
                    soft["slug"], soft["tries"] = row["slug"], 0
                soft["tries"] += 1
                soft["streak"] += 1
                soft["ok_run"] = 0        # "200 CLEAN fetches" means since the last failure
                pace["s"] = min(pace["s"] * 1.15, 4.0)     # nudge only; the IP change is the fix
                if soft["tries"] > 8:
                    counts["miss"] += 1
                    append_jsonl(ANOM, [{"slug": row["slug"],
                                         "code": "fetch_failed_after_retries",
                                         "detail": str(st)}])
                    log(f"  MISS (gave up after 8 tries) {row['slug']} status={st}")
                    soft["slug"], soft["tries"] = None, 0
                    i += 1
                    time.sleep(pace["s"])
                    continue
                # A 503 here is a PER-IP rate limit: every wave observed cleared the
                # instant we changed IP, so waiting it out is pure lost time. Rotate on
                # the FIRST failure. rotate_vpn() still falls back to the silent cooldown
                # if every region is blocked, which is the only case waiting helps.
                log(f"  SOFT {st} after {soft.get('since_rotate', 0)} events on this IP "
                    f"-> rotating immediately (try {soft['tries']}, pace {pace['s']:.1f}s)")
                rotate_vpn(f"soft-{st}")
                soft["streak"] = 0
                continue                          # retry SAME event

            html = res["html"]
            if "primaryDetailsContainer" not in html and "sectionFightCard" not in html:
                log(f"  BAD PAGE (no event markup) {row['slug'][:40]} -> rotating, retry")
                rotate_vpn("bad-page")
                continue                      # retry SAME event, nothing written
            p = html_path(row["slug"])
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
            with gzip.open(tmp, "wb", compresslevel=6) as f:   # archive BEFORE parsing
                f.write(html.encode("utf-8"))
            os.replace(tmp, p)

            try:
                ev = parse_event(html, url)
                e_row, b_rows, l_rows = flatten(ev, row)
                anoms = qa(ev, row)
                collect_labels(ev, reg)
            except Exception as ex:                # a parser bug must not stop the crawl;
                counts["parse_err"] += 1           # the HTML is already safely on disk
                append_jsonl(ANOM, [{"slug": row["slug"], "code": "parser_exception",
                                     "detail": f"{type(ex).__name__}: {ex}"}])
                log(f"  PARSE ERROR {row['slug']}: {type(ex).__name__}: {ex}")
                done.add(row["slug"]); done_buf.append(row["slug"])
                i += 1
                time.sleep(pace["s"])
                continue

            append_jsonl(DETAILS, [e_row])
            append_jsonl(BOUTS, b_rows)
            append_jsonl(EFIGHT, l_rows)
            if anoms:
                append_jsonl(ANOM, anoms)
                counts["anom"] += len(anoms)
            counts["ok"] += 1
            counts["bouts"] += len(b_rows)
            soft["streak"] = 0
            soft["ok_run"] = soft.get("ok_run", 0) + 1
            soft["since_rotate"] = soft.get("since_rotate", 0) + 1
            if soft["ok_run"] >= 200 and pace["s"] > args.sleep:
                pace["s"] = max(args.sleep, pace["s"] * 0.8)   # creep back toward normal
                soft["ok_run"] = 0
                log(f"  200 clean fetches -> pace back to {pace['s']:.1f}s")
            done.add(row["slug"]); done_buf.append(row["slug"])
            i += 1
            soft["slug"] = None
            if counts["ok"] % 25 == 0:
                flush(row["slug"])
                el = time.time() - t0
                rate = counts["ok"] / (el / 60)
                left = len(todo) - counts["ok"]
                eta = left / rate if rate else 0
                done_pct = 100 * len(done) / len(work)
                log(f"  {counts['ok']}/{len(todo)} this run | {len(done)}/{len(work)} total "
                    f"({done_pct:.1f}%) | {counts['bouts']} bouts | {counts['anom']} anom | "
                    f"{rate:.1f} ev/min | ETA {eta/60:.1f}h | at {row['date']} "
                    f"{row['slug'][:38]}")
            time.sleep(pace["s"])

        flush()
        el = time.time() - t0
        log(f"STOP: {counts['ok']} events, {counts['bouts']} bouts, "
            f"{counts['anom']} anomalies, {counts['miss']} misses, "
            f"{counts['parse_err']} parser errors in {el/60:.1f} min "
            f"({counts['ok']/(el/60):.1f} ev/min). done={len(done)}/{len(work)}")
        close_browser()

if __name__ == "__main__":
    main()
