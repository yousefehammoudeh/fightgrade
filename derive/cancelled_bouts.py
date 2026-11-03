#!/usr/bin/env python3
"""Recover CANCELLED TITLE FIGHTS from the cached event pages.

Tapology prints a "Cancelled & Fizzled Bouts" block on the event page, and the stage-3
parser already captured it into `event_details.jsonl -> cancelled_bouts` with the status
and the reason ("Aldo Rib Injury"). What it did not capture is whether the bout was for a
title: that is encoded purely as a championship-belt SVG flanking the status pill, with no
text anywhere in the row. So a cancelled title fight is indistinguishable from any other
cancelled bout in the parsed data.

The belt is recoverable without a crawl -- `data/html/` holds the pages. Inside
`id='sectionCancelled'` each row is `id='bout<ID>Cancelled'`, and a title bout carries
`<svg class="... fill-tap_darkgold" ...>` twice, once each side of the pill. Nothing else
in the block uses that class on an svg.

UFC 189 is the worked example: Aldo vs McGregor scores 2 belts, the other five cancelled
bouts on the card score 0.

  python3 derive/cancelled_bouts.py   ->  data/cancelled_title_bouts.jsonl
"""
import gzip, json, re, sys, collections, hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA, HTML = ROOT / "data", ROOT / "data" / "html"

SECTION = "id='sectionCancelled'"
ROW = re.compile(r"id='bout(\d+)Cancelled'")
BELT = re.compile(r'<svg[^>]*fill-tap_darkgold')
NEXT_SECTION = re.compile(r"id='section(?!Cancelled)")


def belts_by_bout(html):
    """-> {bout_id: belt_svg_count} for the cancelled section, or {} if there is none."""
    i = html.find(SECTION)
    if i < 0:
        return {}
    sec = html[i:]
    m = NEXT_SECTION.search(sec, len(SECTION))
    if m:
        sec = sec[:m.start()]
    rows = list(ROW.finditer(sec))
    out = {}
    for k, r in enumerate(rows):
        end = rows[k + 1].start() if k + 1 < len(rows) else len(sec)
        out[int(r.group(1))] = len(BELT.findall(sec[r.start():end]))
    return out


WEIGHT = re.compile(r"Weight:</span>\s*<span[^>]*>\s*([\d.]+)\s*lbs", re.I)
BILLING = re.compile(r"text-tap_gold uppercase font-bold'>([^<]{3,120})</span>")
LEVEL = re.compile(r"Pro/Am:</span>\s*<span[^>]*>\s*([^<]{3,30}?)\s*</span>", re.I)
DIVWORD = re.compile(r"\b((?:light|super|junior)\s+)?([a-z]+weight)\b", re.I)
INTERIM = re.compile(r"\bInterim\b", re.I)
VACANTW = re.compile(r"\b(Vacant|Inaugural)\b", re.I)
# The crawler takes `sport` from the worklist row, and a cancelled bout has no worklist row
# to take it from -- so all 8,101 came back with sport None. The page states it twice, in
# the <title> and in the status span. Checked against every non-MMA sport in the corpus:
# Boxing, Bare Knuckle, Muay Thai, Kickboxing, Grappling, Lethwei, Sanda, Pancrase and the
# rest all agree with the independently derived `sport`, so this is a read, not a guess.
SPORT_TITLE = re.compile(
    r"<title>[^<]*\|\s*([A-Za-z][A-Za-z \-()]*?)\s*Bout\s*\|\s*Tapology</title>", re.I)
SPORT_SPAN = re.compile(
    r">\s*(?:Cancelled|Fizzled)\s+([A-Za-z][A-Za-z \-()]*?)\s+Bout\s*<", re.I)
HTMLB = None


def bout_html(slug):
    h = hashlib.sha1(slug.encode()).hexdigest()
    p = HTMLB / h[:2] / h[2:4] / (slug + ".html.gz")
    if not p.exists():
        return None
    try:
        return gzip.open(p, "rt", errors="replace").read()
    except Exception:
        return None


def enrich(rows):
    """Fold in what the bout-page crawl recovered.

    The cancelled block on the EVENT page gives only the fighters, the status and the
    reason. The BOUT page gives the title on the line, the contracted weight and often the
    belt status -- which is why the 8,101 pages were worth crawling.
    """
    global HTMLB
    HTMLB = DATA / "html_bouts"
    det = {}
    for line in (DATA / "bout_details.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line); det[d["bout_slug"]] = d
    n = collections.Counter()
    for r in rows:
        d = det.get(r["bout_slug"])
        if not d:
            n["no bout page"] += 1
            continue
        n["enriched"] += 1
        t = d.get("title_on_line") or d.get("title_bout_from_card")
        r["title_on_line"] = t
        r["belt_status_before_fight"] = d.get("belt_status_before_fight")
        r["belt_status_kind"] = d.get("belt_status_kind")
        r["belt_holder"] = d.get("belt_holder")
        if t:
            m = DIVWORD.search(t)
            r["division"] = m.group(0).strip().lower() if m else None
            r["interim"] = bool(INTERIM.search(t))
            r["vacant_title"] = bool(VACANTW.search(t))
            if "Tournament" in t:
                n["tournament final"] += 1
            else:
                n["divisional title"] += 1
        html = bout_html(r["bout_slug"])
        if html:
            w = WEIGHT.search(html)
            if w:
                r["weight_lb"] = float(w.group(1)); n["weight"] += 1
            b = BILLING.search(html)
            if b:
                r["billing"] = re.sub(r"\s+", " ", b.group(1)).strip()
            sp = SPORT_TITLE.search(html) or SPORT_SPAN.search(html)
            if sp:
                r["sport"] = sp.group(1).strip(); n["sport"] += 1
            lv = LEVEL.search(html)
            if lv:
                r["level"] = lv.group(1).strip(); n["level"] += 1
    for k, v in sorted(n.items()):
        print(f"  {k:<32} {v:>8,}")
    return rows


def main():
    # only pages that actually have a cancelled bout are worth opening
    want = {}
    for line in (DATA / "event_details.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip() or "cancelled_bouts" not in line:
            continue
        d = json.loads(line)
        cb = d.get("cancelled_bouts") or []
        if cb:
            want[d["event_slug"]] = d
    print(f"events with a cancelled/fizzled bout: {len(want):,}")

    index = {}
    for p in HTML.rglob("*.html.gz"):
        index[p.name[:-len(".html.gz")]] = p

    out, stats = [], collections.Counter()
    for slug, d in want.items():
        p = index.get(slug)
        if not p:
            stats["no cached page"] += 1
            continue
        try:
            html = gzip.open(p, "rt", errors="replace").read()
        except Exception:
            stats["unreadable page"] += 1
            continue
        belts = belts_by_bout(html)
        if not belts:
            stats["no cancelled section in html"] += 1
            continue
        stats["pages read"] += 1
        pid = (d.get("promotion") or {}).get("promotion_id")
        for cb in d.get("cancelled_bouts") or []:
            n = belts.get(cb.get("bout_id"), 0)
            stats["title" if n else "non-title"] += 1
            if not n:
                continue
            out.append({
                "bout_slug": cb.get("bout_slug"), "bout_id": cb.get("bout_id"),
                "event_slug": slug, "event_name": d.get("event_name"),
                "event_date": d.get("event_date_index"),
                "promotion_id": pid, "sport": d.get("sport"),
                "status": cb.get("status"), "reason": cb.get("reason"),
                "fighters": cb.get("fighters"), "belt_icons": n,
            })

    out = enrich(out)
    out.sort(key=lambda r: (r["event_date"] or "", r["bout_slug"] or ""))
    with (DATA / "cancelled_title_bouts.jsonl").open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"cancelled TITLE bouts recovered: {len(out):,}")
    for k, v in sorted(stats.items()):
        print(f"  {k:<32} {v:>8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
