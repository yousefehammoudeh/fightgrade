#!/usr/bin/env python3
"""Rebuild every output table from the archived HTML -- no network, no re-crawl.

The crawl spans days and the extractor improves during it, so rows written early lack
fields added later. This replays the CURRENT extractor over the whole archive so the
dataset is uniform regardless of which code version fetched each page.

Parsing one page costs ~124 ms and each page is independent, so the work is spread over
a process pool. Results come back through imap in submission order, so the output files
are byte-identical to the serial version -- --jobs only changes how long it takes.

Writes to *.rebuilt.jsonl first and only swaps them in when the run completes and the
row counts pass sanity checks, so an interrupted re-parse cannot damage a good dataset.

  python3 -m pipeline.tapology_archive.reparse [--limit N] [--jobs N] [--out-dir D] [--commit]
"""
import argparse, gzip, json, os, sys, time, collections, pathlib
import multiprocessing as mp

ROOT = pathlib.Path(__file__).resolve().parents[2]
from pipeline.parsers.tapology import parse_event             # noqa: E402
from pipeline.tapology_archive import crawl_stage3 as c3       # noqa: E402  reuse flatten()/qa()

DATA, STATE = ROOT / "data", ROOT / "logs"


def work(task):
    """Parse one archived page. Returns pre-serialised lines so the parent only writes.

    Serialising in the worker keeps the parent off the critical path -- it would
    otherwise become the bottleneck well before the pool saturates.
    """
    path, slug, event_id, promotion_id, raw_row_text, year = task
    try:
        html = gzip.open(path, "rb").read().decode("utf-8", errors="replace")
    except Exception as ex:
        return ("unreadable", None, (), (),
                (json.dumps({"slug": slug, "code": "archive_unreadable",
                             "detail": type(ex).__name__}),))
    row = {"slug": slug, "event_id": event_id,
           "date": "", "date_precision": "", "promotion_id": promotion_id}
    d, prec = c3.event_date(raw_row_text, year)
    row["date"], row["date_precision"] = d.isoformat(), prec
    anoms = []
    try:
        ev = parse_event(html, "https://www.tapology.com/fightcenter/events/" + slug)
        e_row, b_rows, l_rows = c3.flatten(ev, row)
        for a in c3.qa(ev, row):
            anoms.append(json.dumps(a, ensure_ascii=False))
    except Exception as ex:
        anoms.append(json.dumps({"slug": slug, "code": "parser_exception",
                                 "detail": f"{type(ex).__name__}: {ex}"}))
        return ("parse_error", None, (), (), tuple(anoms))
    return ("ok",
            json.dumps(e_row, ensure_ascii=False),
            tuple(json.dumps(b, ensure_ascii=False) for b in b_rows),
            tuple(json.dumps(l, ensure_ascii=False) for l in l_rows),
            tuple(anoms))


def tasks(files, idx, year):
    """Only the three index fields the parse needs cross the process boundary --
    shipping the whole 66 MB index to every worker would cost more than the parse."""
    for f in files:
        slug = f.name[:-8]
        src = idx.get(slug, {})
        yield (str(f), slug, src.get("event_id"), src.get("promotion_id"),
               src.get("raw_row_text"), year)


def merge_slugs(path, key, targets, new_lines):
    """Replace every row belonging to `targets` with `new_lines`, leaving the rest byte
    for byte as they were. Rewrites via a temp file so an interruption cannot truncate
    a good table."""
    tmp = path.with_name(path.name + ".merge")
    kept = dropped = 0
    with path.open(encoding="utf-8") as fin, tmp.open("w", encoding="utf-8") as fout:
        for line in fin:
            st = line.strip()
            if st:
                try:
                    if json.loads(st).get(key) in targets:
                        dropped += 1
                        continue
                except Exception:
                    pass
            fout.write(line)
            kept += 1
        for l in new_lines:
            fout.write(l + "\n")
    os.replace(tmp, path)
    return kept, dropped, len(new_lines)


def run_only(slugs, idx, year):
    """Re-parse a handful of events and splice them into the existing tables.

    Same work() as the full run, so the rows are identical to what a full re-parse
    would produce -- this only avoids replaying the other 104k pages.
    """
    archive = {p.name[:-8]: p for p in DATA.joinpath("html").rglob("*.html.gz")}
    missing = [s for s in slugs if s not in archive]
    if missing:
        print(f"  no archived page for: {', '.join(missing)}")
        return 1
    targets = set(slugs)
    det, bts, lnk, anm = [], [], [], []
    for res in map(work, tasks([archive[s] for s in slugs], idx, year)):
        status, e_line, b_lines, l_lines, a_lines = res
        anm.extend(a_lines)
        if status != "ok":
            print(f"  FAILED ({status}) -- aborting, nothing written")
            for a in a_lines:
                print("   ", a)
            return 1
        det.append(e_line); bts.extend(b_lines); lnk.extend(l_lines)
    print(f"  parsed {len(det)} event(s): {len(bts)} bouts, {len(lnk)} fighter links, "
          f"{len(anm)} anomalies")
    for path, key, new in (
            (DATA/"event_details.jsonl",  "event_slug", det),
            (DATA/"bouts.jsonl",          "event_slug", bts),
            (DATA/"event_fighters.jsonl", "event_slug", lnk),
            (STATE/"anomalies.jsonl",     "slug",       anm)):
        kept, dropped, added = merge_slugs(path, key, targets, new)
        print(f"  {path.name:<24} kept {kept}  replaced {dropped}  added {added}"
              f"  -> {kept + added}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--jobs", type=int, default=0,
                    help="worker processes (default: cpu_count-3, 1 = serial)")
    ap.add_argument("--chunk", type=int, default=16, help="tasks dispatched per batch")
    ap.add_argument("--out-dir", default=None, help="where to write *.rebuilt.jsonl")
    ap.add_argument("--only", nargs="+", metavar="SLUG",
                    help="re-parse just these event slugs and splice them into\n"
                         "the existing tables, instead of replaying the archive")
    ap.add_argument("--commit", action="store_true",
                    help="swap the rebuilt files in (default: dry run)")
    args = ap.parse_args()
    jobs = args.jobs if args.jobs else max(1, (os.cpu_count() or 2) - 3)
    outdir = pathlib.Path(args.out_dir) if args.out_dir else DATA
    anomdir = pathlib.Path(args.out_dir) if args.out_dir else STATE

    # the index gives each event its date/promotion context
    idx = {}
    for line in (DATA/"events.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                e = json.loads(line)
                idx[e["event_slug"]] = e
            except Exception:
                pass

    if args.only:
        return run_only(args.only, idx, time.localtime().tm_year)

    files = sorted(DATA.joinpath("html").rglob("*.html.gz"))
    if args.limit:
        files = files[:args.limit]
    out = {k: (outdir / f"{k}.rebuilt.jsonl").open("w", encoding="utf-8")
           for k in ("event_details", "bouts", "event_fighters")}
    anom = (anomdir / "anomalies.rebuilt.jsonl").open("w", encoding="utf-8")
    counts = collections.Counter()
    year = time.localtime().tm_year
    t0 = time.time()
    print(f"  re-parsing {len(files)} archived pages with {jobs} worker(s)", flush=True)

    pool = None
    if jobs == 1:
        results = map(work, tasks(files, idx, year))
    else:
        pool = mp.Pool(jobs)
        results = pool.imap(work, tasks(files, idx, year), chunksize=args.chunk)

    for i, (status, e_line, b_lines, l_lines, a_lines) in enumerate(results, 1):
        for a in a_lines:
            anom.write(a + "\n")
        if status != "ok":
            counts[status] += 1
        else:
            out["event_details"].write(e_line + "\n")
            for b in b_lines:
                out["bouts"].write(b + "\n")
            for l in l_lines:
                out["event_fighters"].write(l + "\n")
            counts["events"] += 1
            counts["bouts"] += len(b_lines); counts["links"] += len(l_lines)
        if i % 2000 == 0:
            el = time.time() - t0
            print(f"  {i}/{len(files)}  {counts['bouts']} bouts  "
                  f"{i/el:.0f} ev/s  eta {(len(files)-i)/(i/el)/60:.0f} min", flush=True)

    if pool is not None:
        pool.close(); pool.join()
    for f_ in out.values(): f_.close()
    anom.close()
    el = time.time() - t0
    print(f"\nre-parsed {counts['events']} events, {counts['bouts']} bouts, "
          f"{counts['links']} fighter links in {el/60:.1f} min "
          f"({counts['parse_error']} parse errors, {counts['unreadable']} unreadable)")

    # sanity: never swap in a dataset that lost events or bouts
    old_ev = sum(1 for _ in (DATA/"event_details.jsonl").open()) \
             if (DATA/"event_details.jsonl").exists() else 0
    old_b = sum(1 for _ in (DATA/"bouts.jsonl").open()) if (DATA/"bouts.jsonl").exists() else 0
    print(f"  events {old_ev} -> {counts['events']}   bouts {old_b} -> {counts['bouts']}")
    ok = counts["events"] >= old_ev * 0.99 and counts["bouts"] >= old_b * 0.99
    if not ok:
        print("  REFUSING TO COMMIT: rebuilt dataset is smaller than the current one")
        return 1
    if not args.commit:
        print("  dry run -- rerun with --commit to swap the rebuilt files in")
        return 0
    for k in out:
        os.replace(outdir/f"{k}.rebuilt.jsonl", DATA/f"{k}.jsonl")
    os.replace(anomdir/"anomalies.rebuilt.jsonl", STATE/"anomalies.jsonl")
    print("  committed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
