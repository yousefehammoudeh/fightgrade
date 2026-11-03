#!/usr/bin/env python3
"""Per-fighter weights from the archived bout pages.

The bout page carries a comparison table the pipeline never read. Three of its rows are
weights, and they are different things:

  Weigh-In Result    what each fighter scaled at the official weigh-in
  Fight Day Weight   what they walked to the cage at, after rehydrating
  Weight Gain        the difference

They answer a question the contracted weight cannot: whether the fighters were anywhere
near the class the bout was billed at. UFC 227 reads 124.5 and 125.0 lbs for a 125 lb
flyweight title -- both made it -- then 141.0 and 142.0 on fight day, a 13-14% rehydration.

Coverage is partial and worth knowing before relying on it: of the title bouts with a
cached page, ~43% carry a weigh-in and almost none carry a fight-day weight (it is close to
UFC-only). Nothing is inferred where the row is absent.

  python3 derive/weigh_ins.py   ->  data/bout_weigh_ins.jsonl
"""
import collections, gzip, hashlib, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
HTMLB = DATA / "html_bouts"

TAG = re.compile(r"<[^>]+>")
LBS = re.compile(r"([\d.]+)\s*lbs")
WANT = ("Weigh-In Result", "Fight Day Weight")


def page(slug):
    h = hashlib.sha1(slug.encode()).hexdigest()
    p = HTMLB / h[:2] / h[2:4] / (slug + ".html.gz")
    if not p.exists():
        return None
    try:
        return gzip.open(p, "rt", errors="replace").read()
    except Exception:
        return None


def parse(html):
    """-> {label: (left_lb, right_lb)}. The table is left | label | right, and each side
    is printed twice (a desktop cell and a mobile one), so take the first and the last."""
    out = {}
    for tr in re.split(r"<tr\b", html):
        if not any(w in tr for w in WANT):
            continue
        cells = [re.sub(r"\s+", " ", TAG.sub(" ", t)).strip()
                 for t in re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.S)]
        cells = [c for c in cells if c]
        lab = next((w for w in WANT if w in cells), None)
        if not lab:
            continue
        vals = [float(m.group(1)) for m in (LBS.search(c) for c in cells if c != lab) if m]
        if len(vals) >= 2:
            out[lab] = (vals[0], vals[-1])
    return out


def main():
    seen, rows = set(), []
    for line in (DATA / "bout_title_stakes.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            if r["bout_slug"] not in seen:
                seen.add(r["bout_slug"])
                rows.append((r["bout_slug"], r.get("weight_lb")))

    n = collections.Counter()
    with (DATA / "bout_weigh_ins.jsonl").open("w", encoding="utf-8") as f:
        for slug, contracted in rows:
            html = page(slug)
            if not html:
                n["no cached page"] += 1
                continue
            w = parse(html)
            if not w:
                n["page has no weight rows"] += 1
                continue
            wi = w.get("Weigh-In Result")
            fd = w.get("Fight Day Weight")
            n["weigh-in"] += bool(wi)
            n["fight-day"] += bool(fd)
            rec = {"bout_slug": slug, "contracted_lb": contracted}
            if wi:
                rec["weigh_in_lb"] = list(wi)
                if contracted:
                    over = [round(x - contracted, 1) for x in wi]
                    rec["over_lb"] = over
                    # a full pound over is the usual missed-weight threshold; the 1 lb
                    # allowance on a non-title bout does not apply to a title fight
                    if max(over) > 1.0:
                        n["someone over the limit"] += 1
            if fd:
                rec["fight_day_lb"] = list(fd)
                if wi:
                    rec["rehydration_lb"] = [round(a - b, 1) for a, b in zip(fd, wi)]
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"title bouts examined: {len(rows):,}")
    for k, v in sorted(n.items()):
        print(f"  {k:<28} {v:>7,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
