#!/usr/bin/env python3
"""Stage-3b crawler: TITLE bout pages -> raw HTML archive + belt-status records.

Copy of crawl_stage3.py per docs/stage3b-title-bouts.md; the browser, VPN pool,
rotation, checkpointing and archive machinery are unchanged because they are proven
over 104k pages. Four things differ: the worklist (title bouts, not events), the URL
path, the extractor, and the outputs.

Captures BELT STATUS (Title on Line, Belt Status Before Fight) and TOURNAMENT
PLACEMENT (Tournament, Tournament Round). Gym and "Fighting out of" are deliberately
not kept.

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
  python3 scrapers/driver/crawl_stage3.py [--limit N] [--sleep SECS] [--newest-first]
"""
import argparse, gzip, hashlib, json, os, re, shutil, signal, subprocess, sys, tempfile, time
from collections import Counter
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scrapers" / "extract"))
from tapology_extract import parse_bout_page                  # noqa: E402
from playwright.sync_api import sync_playwright                # noqa: E402
from playwright._impl._errors import TargetClosedError, Error as PWError  # noqa: E402

STATE   = ROOT / "logs"
STATE_DIR = STATE
BOUTS_IN = ROOT / "data" / "bouts.jsonl"          # worklist source
EVDET_IN = ROOT / "data" / "event_details.jsonl"  # for event dates / status
HTMLDIR = ROOT / "data" / "html_bouts"            # NOT data/html -- that is the event archive
DETAILS = ROOT / "data" / "bout_details.jsonl"
ANOM    = STATE / "anomalies_bouts.jsonl"
LABELS  = STATE / "labels_bouts.json"
DONE    = STATE / "bouts_done.txt"
STATUS  = STATE / "status3b.json"
LOG     = STATE / "crawl3b.log"

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
    const r = await fetch(u, {credentials:"include"});
    const b = await r.text();
    if (/Just a moment/i.test(b)) return {status:"CHALLENGE", html:null};
    // A dead bout slug returns 200 and silently serves /fightcenter. No IP change can
    // fix that, so it must not look like a block. docs/stage3b-title-bouts.md warns
    // about this for CONSTRUCTED slugs -- Tapology also emits broken ones itself.
    if (r.url && r.url.replace(/\/$/,"") !== u.replace(/\/$/,""))
      return {status:"REDIRECT", html:null, to:r.url};
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

def html_path(slug):
    h = hashlib.sha1(slug.encode()).hexdigest()
    return HTMLDIR / h[:2] / h[2:4] / (slug + ".html.gz")

def load_done():
    if not DONE.exists():
        return set()
    return {l.strip() for l in DONE.read_text().splitlines() if l.strip()}

def build_worklist(newest_first=False, completed_only=False, any_kind=False):
    """Title bouts only. `title_kind == "title"` excludes `"tournament"`, which is
    bracket rounds (IBJJF finals, ADCC, Olympic draws) carrying no belt information."""
    ev = {}
    for line in EVDET_IN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        ev[d["event_slug"]] = (d.get("event_date_index") or "9999-12-31",
                               d.get("status"))
    rows, seen = [], set()
    for line in BOUTS_IN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            b = json.loads(line)
        except Exception:
            continue                       # tolerate a torn line rather than crash-loop
        # An explicit --worklist may name tournament finals that founded a lineage
        # (see docs/belts.md); those are not title_kind == "title".
        if not any_kind and b.get("title_kind") != "title":
            continue
        slug = b.get("bout_slug")
        if not slug or slug in seen:       # never construct a slug -- see the doc
            continue
        d, st = ev.get(b.get("event_slug"), ("9999-12-31", None))
        if completed_only and st != "Completed":
            continue
        seen.add(slug)
        rows.append({"date": d, "status": st, "slug": slug,
                     "bout_id": b.get("bout_id"), "event_slug": b.get("event_slug"),
                     "sport": b.get("sport_base"), "title_bout": b.get("title_bout")})
    rows.sort(key=lambda r: (r["date"], r["bout_id"] or 0), reverse=newest_first)
    return rows

# ------------------------------------------------------------------ QA ------
VACANT_RE = re.compile(r"^\s*vacant\s*$", re.I)
HELD_RE   = re.compile(r"^\s*held\s+by\s+(.+?)\s*$", re.I)

def belt_status_kind(v):
    """Three states, not two: held, vacant, or the label absent entirely."""
    if v is None:
        return "absent", None
    if VACANT_RE.match(v):
        return "vacant", None
    m = HELD_RE.match(v)
    if m:
        return "held", m.group(1)
    return "other", v

def qa(b, row):
    out = []
    def flag(code, detail=""):
        out.append({"bout_slug": row["slug"], "code": code, "detail": detail})
    if not b.get("parse_ok"):
        flag("parse_not_ok")
    info = b.get("info") or {}
    if "Title on Line:" not in (b.get("info_raw") or {}):
        # we selected this bout *because* the event page called it a title fight
        flag("no_title_on_line_label", str(row.get("title_bout"))[:80])
    # Test the LABEL, not the value. Tapology renders "Belt Status Before Fight: N/A"
    # on a third of title bouts and val() maps N/A to None, so a value test would flag
    # 34% of a healthy crawl as broken -- and a key test can never fire at all, because
    # the key is present whenever the label is. Only a missing label is a real defect.
    if "Belt Status Before Fight:" not in (b.get("info_raw") or {}):
        flag("no_belt_status_label", row.get("sport") or "")
    kind, _ = belt_status_kind(info.get("belt_status_before_fight"))
    if kind == "other":
        flag("belt_status_unrecognised", str(info.get("belt_status_before_fight"))[:80])
    if len(b.get("fighters") or []) < 2:
        flag("fewer_than_2_fighters", str(len(b.get("fighters") or [])))
    return out

def collect_labels(b, reg):
    """Vocabulary drift detector: new belt-status phrasings show up here first."""
    info = b.get("info") or {}
    for raw in (b.get("info_raw") or {}):
        reg["bout_label"][raw] += 1
    kind, _ = belt_status_kind(info.get("belt_status_before_fight"))
    reg["belt_status_kind"][kind] += 1
    if kind in ("vacant", "other") and info.get("belt_status_before_fight"):
        reg["belt_status_value"][info["belt_status_before_fight"][:60]] += 1

def flatten(b, row):
    """Belt status + tournament placement. Gym / "Fighting out of" stay dropped."""
    info = b.get("info") or {}
    status_raw = info.get("belt_status_before_fight")
    kind, holder = belt_status_kind(status_raw)
    return {
        "bout_slug": row["slug"],
        "bout_id": b.get("bout_id") or row.get("bout_id"),
        "bout_url": b.get("bout_url"),
        "event_slug": (b.get("event") or {}).get("event_slug") or row.get("event_slug"),
        "event_date": row.get("date"),
        "event_status": row.get("status"),
        "sport": row.get("sport"),
        "title_bout_from_card": row.get("title_bout"),
        "title_on_line": info.get("title_on_line"),
        "belt_status_before_fight": status_raw,
        "belt_status_kind": kind,
        "belt_holder": holder,
        # NOTE on kind == "absent": val() maps the literal "N/A" Tapology prints to
        # None, so "absent" could in principle mean either "page said N/A" or "no
        # such label". Measured 2026-08-25 over all 46,893 stage-3b rows: the label
        # is present on every single one, and no_belt_status_label has never fired.
        # So on this corpus "absent" ALWAYS means the page explicitly said N/A --
        # i.e. no belt existed going in (inaugural title bout / tournament final).
        # A separate label_present flag was tried and dropped: it was constant True.
        # Tournament placement. Already produced by the extractor from the same
        # label/value list as the belt fields -- previously discarded here.
        "tournament": info.get("tournament"),
        "tournament_round": info.get("tournament_round"),
        "fighters": [f.get("fighter_slug") for f in (b.get("fighters") or [])],
        "parse_ok": bool(b.get("parse_ok")),
        "scraped_at": now(),
    }

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
    ap.add_argument("--newest", type=int, default=0,
                    help="take the N most recent title bouts")
    ap.add_argument("--oldest", type=int, default=0,
                    help="take the N oldest title bouts (combines with --newest)")
    ap.add_argument("--completed-only", action="store_true",
                    help="only bouts on events with status Completed")
    ap.add_argument("--include-unindexed", action="store_true",
                    help="crawl --worklist slugs that are absent from bouts.jsonl "
                         "(cancelled bouts never appear on a card)")
    ap.add_argument("--worklist", default=None,
                    help="file of bout slugs (one per line) to restrict the queue to; "
                         "produced by scrapers/derive/belts.py")
    ap.add_argument("--rotate-every", type=int, default=600,
                    help="events per IP before a proactive rotation")
    args = ap.parse_args()

    for p in (HTMLDIR, STATE):
        p.mkdir(parents=True, exist_ok=True)

    # Single-instance guard. Two crawlers sharing the append-only outputs produce
    # duplicate rows and race on the archive temp file, so refuse to start if another
    # one is alive. (A stale pid from a hard kill is ignored.)
    LOCK = STATE / "crawl_stage3b.lock"
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

    work = build_worklist(args.newest_first, args.completed_only, any_kind=bool(args.worklist))
    if args.worklist:
        want = {l.strip() for l in open(args.worklist) if l.strip()}
        before = len(work)
        work = [r for r in work if r["slug"] in want]
        log(f"  worklist filter: {before} -> {len(work)} bouts "
            f"({len(want)} slugs requested, {len(want) - len(work)} not in the index)")
        if args.include_unindexed:
            # A CANCELLED bout never appears on the card, so it is not in bouts.jsonl and
            # the index cannot supply a row for it. The slug is still a real page and it
            # carries its own bout_id as the leading number, which is all the crawler
            # needs. Date is unknown, so these sort to the end of the queue.
            have = {r["slug"] for r in work}
            added = 0
            for slug in sorted(want - have):
                m = re.match(r"^(\d+)-", slug)
                work.append({"date": "9999-12-31", "status": None, "slug": slug,
                             "bout_id": int(m.group(1)) if m else None,
                             "event_slug": None, "sport": None, "title_bout": None})
                added += 1
            log(f"  --include-unindexed: added {added} slugs absent from bouts.jsonl")
    done = load_done()
    todo = [r for r in work if r["slug"] not in done]
    # Pilot selection: the two ends of the date range, where page format is most likely
    # to differ. Taken from the date-sorted worklist before any other filtering.
    if args.newest or args.oldest:
        by_date = sorted(todo, key=lambda r: (r["date"], r["bout_id"] or 0))
        picked, seen = [], set()
        for r in (by_date[-args.newest:] if args.newest else []):
            if r["slug"] not in seen:
                seen.add(r["slug"]); picked.append(r)
        for r in (by_date[:args.oldest] if args.oldest else []):
            if r["slug"] not in seen:
                seen.add(r["slug"]); picked.append(r)
        todo = sorted(picked, key=lambda r: (r["date"], r["bout_id"] or 0))
    if args.limit:
        todo = todo[:args.limit]
    log(f"START stage-3b: {len(work)} title bouts indexed, {len(done)} done, "
        f"{len(todo)} queued this run")
    if not todo:
        # completion flag: `remaining` never reaches 0 on its own because future-dated
        # events are deliberately never crawled, so the queue emptying is the real signal
        (STATE / "stage3b.done").write_text(now())
        log("STAGE 3B COMPLETE (queue exhausted)")
        return
    if todo:
        log(f"  first: {todo[0]['date']}  {todo[0]['slug']}")
        log(f"  last : {todo[-1]['date']}  {todo[-1]['slug']}")

    reg = {k: Counter() for k in
           ("bout_label", "belt_status_kind", "belt_status_value")}
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
    badpage = {"slug": None, "tries": 0}
    BADPAGE_MAX = 3
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
            url = "https://www.tapology.com/fightcenter/bouts/" + row["slug"]
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
                if st == "REDIRECT":              # dead slug: permanent, never retry
                    counts["miss"] += 1
                    append_jsonl(ANOM, [{"bout_slug": row["slug"], "code": "redirected",
                                         "detail": str(res.get("to"))[:120]}])
                    log(f"  MISS {row['slug'][:44]} -> redirected to {res.get('to')}")
                    done.add(row["slug"]); done_buf.append(row["slug"])
                    i += 1
                    time.sleep(pace["s"])
                    continue
                if st in (404, 410):              # genuinely gone: skip for good
                    counts["miss"] += 1
                    append_jsonl(ANOM, [{"bout_slug": row["slug"], "code": "fetch_failed",
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
                    append_jsonl(ANOM, [{"bout_slug": row["slug"],
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
            if "boutComparisonTable" not in html and "Bout Information" not in html:
                # Bounded. An unbounded retry here spun 75 times on one dead slug,
                # burning a VPN region per attempt, until the run was killed by hand.
                if badpage.get("slug") != row["slug"]:
                    badpage["slug"], badpage["tries"] = row["slug"], 0
                badpage["tries"] += 1
                if badpage["tries"] > BADPAGE_MAX:
                    counts["miss"] += 1
                    append_jsonl(ANOM, [{"bout_slug": row["slug"],
                                         "code": "bad_page_persistent",
                                         "detail": f"no bout markup after {BADPAGE_MAX} IPs"}])
                    log(f"  MISS (bad page on {BADPAGE_MAX} IPs) {row['slug'][:44]}")
                    badpage["slug"], badpage["tries"] = None, 0
                    done.add(row["slug"]); done_buf.append(row["slug"])
                    i += 1
                    time.sleep(pace["s"])
                    continue
                log(f"  BAD PAGE (no bout markup) {row['slug'][:40]} "
                    f"-> rotating, retry {badpage['tries']}/{BADPAGE_MAX}")
                rotate_vpn("bad-page")
                continue                      # retry SAME event, nothing written
            p = html_path(row["slug"])
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
            with gzip.open(tmp, "wb", compresslevel=6) as f:   # archive BEFORE parsing
                f.write(html.encode("utf-8"))
            os.replace(tmp, p)

            try:
                bp = parse_bout_page(html, url)
                e_row = flatten(bp, row)
                anoms = qa(bp, row)
                collect_labels(bp, reg)
            except Exception as ex:                # a parser bug must not stop the crawl;
                counts["parse_err"] += 1           # the HTML is already safely on disk
                append_jsonl(ANOM, [{"bout_slug": row["slug"], "code": "parser_exception",
                                     "detail": f"{type(ex).__name__}: {ex}"}])
                log(f"  PARSE ERROR {row['slug']}: {type(ex).__name__}: {ex}")
                done.add(row["slug"]); done_buf.append(row["slug"])
                i += 1
                time.sleep(pace["s"])
                continue

            append_jsonl(DETAILS, [e_row])
            if anoms:
                append_jsonl(ANOM, anoms)
                counts["anom"] += len(anoms)
            counts["ok"] += 1
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
                    f"({done_pct:.1f}%) | {counts['anom']} anom | "
                    f"{rate:.1f} bouts/min | ETA {eta/60:.1f}h | at {row['date']} "
                    f"{row['slug'][:38]}")
            time.sleep(pace["s"])

        flush()
        el = time.time() - t0
        log(f"  belt status: {dict(reg['belt_status_kind'])}")
        log(f"STOP: {counts['ok']} bouts, {counts['anom']} anomalies, "
            f"{counts['miss']} misses, {counts['parse_err']} parser errors "
            f"in {el/60:.1f} min ({counts['ok']/(el/60):.1f} bouts/min). "
            f"done={len(done)}/{len(work)}")
        close_browser()

if __name__ == "__main__":
    main()
