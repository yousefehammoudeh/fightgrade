#!/usr/bin/env python3
"""Stage-3b re-parse: rebuild data/bout_details.jsonl from the archived bout HTML.

The stage-3b counterpart to reparse.py, which only covers stage 3 (events). Same
contract: no network, no re-crawl, replay the CURRENT extractor over
data/html_bouts/ so every row carries the fields the parser produces today,
regardless of which code version fetched the page.

Only parser-derived fields are recomputed. Card-level context (event_date,
event_status, sport, title_bout_from_card) came from the event index at crawl
time, not from the bout page, so it is carried across verbatim from the existing
rows -- a re-parse must not invent or drop it.

Writes bout_details.rebuilt.jsonl and only swaps it in under --commit, after the
row count and key fields are checked against the existing file.

  python3 scrapers/driver/reparse3b.py [--limit N] [--jobs N] [--commit]
"""
import argparse, gzip, json, sys, time, pathlib, collections
import multiprocessing as mp

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scrapers" / "extract"))
from tapology_extract import parse_bout_page                   # noqa: E402
import importlib.util
spec = importlib.util.spec_from_file_location("c3b", ROOT/"scrapers"/"driver"/"crawl_stage3b.py")
c3b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c3b)                                   # reuse flatten()/qa()

DATA = ROOT / "data"
SRC  = DATA / "bout_details.jsonl"
OUT  = DATA / "bout_details.rebuilt.jsonl"

# Fields that come from the event index, not the bout page: carried, never re-derived.
CARRIED = ("event_date", "event_status", "sport", "title_bout_from_card")
BOUT_URL = "https://www.tapology.com/fightcenter/bouts/"


def work(task):
    path, prev = task
    row = {"slug": prev["bout_slug"], "bout_id": prev.get("bout_id"),
           "event_slug": prev.get("event_slug"), "date": prev.get("event_date"),
           "status": prev.get("event_status"), "sport": prev.get("sport"),
           "title_bout": prev.get("title_bout_from_card")}
    try:
        html = gzip.open(path, "rb").read().decode("utf-8", errors="replace")
    except Exception as ex:
        return ("unreadable", prev["bout_slug"], None)
    try:
        # the URL matters: parse_bout_page derives bout_url/bout_id/bout_slug from it,
        # and passing html alone silently nulls them (mirrors reparse.py's parse_event)
        b = parse_bout_page(html, BOUT_URL + prev["bout_slug"])
        out = c3b.flatten(b, row)
    except Exception as ex:
        return ("parse_error", prev["bout_slug"], f"{type(ex).__name__}: {ex}")
    out["scraped_at"] = prev.get("scraped_at")        # preserve original fetch time
    return ("ok", prev["bout_slug"], json.dumps(out, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--jobs", type=int, default=max(1, mp.cpu_count() - 1))
    ap.add_argument("--commit", action="store_true")
    a = ap.parse_args()

    prevs = [json.loads(l) for l in SRC.open()]
    if a.limit:
        prevs = prevs[:a.limit]
    tasks, missing = [], []
    for p in prevs:
        hp = c3b.html_path(p["bout_slug"])
        if hp.exists():
            tasks.append((str(hp), p))
        else:
            missing.append(p["bout_slug"])
    print(f"rows={len(prevs)} archived={len(tasks)} missing_html={len(missing)} jobs={a.jobs}")
    if missing:
        print("  (rows with no archived page are carried through unchanged)")

    t0 = time.time(); stats = collections.Counter(); lines = {}
    with mp.Pool(a.jobs) as pool:
        for i, (st, slug, payload) in enumerate(pool.imap(work, tasks, chunksize=64), 1):
            stats[st] += 1
            if st == "ok":
                lines[slug] = payload
            if i % 5000 == 0:
                print(f"  {i}/{len(tasks)}  {time.time()-t0:.0f}s  {dict(stats)}")
    print(f"done in {time.time()-t0:.0f}s  {dict(stats)}")

    # keep source order; fall back to the previous row when the page could not be redone
    n_new = 0
    with OUT.open("w", encoding="utf-8") as f:
        for p in prevs:
            ln = lines.get(p["bout_slug"])
            if ln is None:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
            else:
                f.write(ln + "\n"); n_new += 1
    print(f"wrote {OUT.name}: {len(prevs)} rows ({n_new} re-parsed)")

    # ---- sanity: row count, and no carried/identity field may drift ----
    old = {p["bout_slug"]: p for p in prevs}
    new = [json.loads(l) for l in OUT.open()]
    assert len(new) == len(prevs), "row count changed"
    # Guard EVERY pre-existing field, not a hand-picked subset. A narrow guard let a
    # nulled bout_url through once; only newly-added keys are expected to differ.
    ADDED = {"tournament", "tournament_round"}
    drift = collections.Counter()
    for r in new:
        o = old[r["bout_slug"]]
        for k in set(o) - ADDED:
            if o.get(k) != r.get(k):
                drift[k] += 1
    print("field drift vs existing:", dict(drift) or "none")
    got = sum(1 for r in new if r.get("tournament"))
    print(f"tournament populated: {got}/{len(new)}")
    if drift:
        print("REFUSING to commit: unexpected drift"); sys.exit(1)

    if a.commit:
        bak = SRC.with_suffix(".jsonl.pre-reparse3b.bak")
        if not bak.exists():
            SRC.replace(bak); print(f"backed up -> {bak.name}")
        OUT.replace(SRC); print(f"committed -> {SRC.name}")
    else:
        print("dry run; re-run with --commit to swap in")


if __name__ == "__main__":
    main()
