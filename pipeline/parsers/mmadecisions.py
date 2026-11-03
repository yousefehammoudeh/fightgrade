"""MMADecisions HTML -> records. Pure functions, no network.

For now only what the crawler needs to find pages: the by-year event index and
the decision links on an event page. The decision-page extractor (judges'
round scores, media scores, fan scoring, tale of the tape) is built against the
archived pages once the table format is agreed.

Every URL needs a slug after the id (`/event/1656` is a 404, any slug works),
so links are kept whole and ids are taken from them. Hrefs on event pages
carry trailing whitespace.
"""
import re
from datetime import datetime

from bs4 import BeautifulSoup

LINK = re.compile(r"^(event|decision|judge|fighter)/(\d+)/([^/?#]*)")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def link(href: str | None) -> tuple[str, str, str] | None:
    """'decision/16424/Grant-Dawson-vs-Nurullo-Aliev ' -> ('decision', '16424', 'Grant-...')."""
    m = LINK.match(clean(href).lstrip("/").removeprefix("https://mmadecisions.com/"))
    return (m.group(1), m.group(2), m.group(3)) if m else None


def parse_date(text: str) -> str | None:
    """'Oct 24, 2026' or 'October 24, 2026' -> '2026-10-24'."""
    t = clean(text)
    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def parse_event_index(html: str) -> list[dict]:
    """/decisions-by-event/[<year>/] -> one dict per event, as listed (newest first):
    id, slug, date and the number of decisions the site holds for it."""
    out = []
    for tr in BeautifulSoup(html, "lxml").select("tr.decision"):
        a = tr.select_one("a[href*='event/']")
        found = link(a.get("href")) if a else None
        if not found:
            continue
        tds = tr.find_all("td")
        n = clean(tds[-1].get_text()) if tds else ""
        out.append({"event_id": found[1], "slug": found[2], "event_name": clean(a.get_text()),
                    "date": parse_date(tds[0].get_text()) if tds else None,
                    "n_decisions": int(n) if n.isdigit() else None})
    return out


def index_years(html: str) -> list[int]:
    """The years the event index links to (the base page is the current year)."""
    return sorted({int(m) for m in re.findall(r"decisions-by-event/(\d{4})/", html)}, reverse=True)


def parse_event_decisions(html: str) -> list[dict]:
    """/event/<id>/<slug> -> the decisions on its card, in page order."""
    out, seen = [], set()
    for a in BeautifulSoup(html, "lxml").select("a[href*='decision/']"):
        found = link(a.get("href"))
        if found and found[0] == "decision" and found[1] not in seen:
            seen.add(found[1])
            out.append({"decision_id": found[1], "slug": found[2]})
    return out


# ------------------------------------------------------------ decision pages
#
# Six tables, all keyed by decision_id (events by event_id). Fighters are in page
# order: the winner first ("X def. Y"), or as listed for a draw. Scores are
# (fighter 1, fighter 2) in that order everywhere.
#
# Old and partial cards: an unnamed judge is "Unknown Judge" with no id; a round
# not known is "-" (None); on early cards the TOTAL is a legend code, not a
# score: 0-0 = unknown, 1-0 = the judge favoured a fighter, score unknown,
# 1-1 = the judge had it a draw. totals_kind says which; a legend card's
# round rows carry the code too, so their scores are None.

LOGO = re.compile(r"resources/img/([A-Za-z0-9_-]+?)_small\.png")
DEDUCTION = re.compile(r"(.+?) was deducted (\d+) points? in round (\d+):?\s*(.*)", re.I)
PIE = re.compile(r"addRows\(\[\s*((?:\[[^\]]*\],?\s*)+)\]\)")


def to_int(text: str | None) -> int | None:
    t = clean(text)
    return int(t) if re.fullmatch(r"-?\d+", t) else None


def pct(text: str | None) -> float | None:
    t = clean(text).rstrip("%")
    try:
        return float(t)
    except ValueError:
        return None


def entity_link(a, kind: str) -> tuple[str | None, str | None]:
    found = link(a.get("href")) if a is not None else None
    return (found[1] if found and found[0] == kind else None), (clean(a.get_text()) if a is not None else None)


def fighter_link(a) -> dict:
    m = re.match(r"\s*/?fighter/(\d+)/", (a.get("href") or "") if a is not None else "")
    return {"fighter_id": m.group(1) if m else None, "name": clean(a.get_text()) if a is not None else None}


def score_pair(text: str) -> tuple[int | None, int | None]:
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", clean(text))
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _tape(s) -> dict:
    """Tale of the tape -> {label: [fighter 1, fighter 2]}, plus nicknames."""
    out, box = {}, None
    for td in s.select("td.top-cell"):
        if "TALE OF THE TAPE" in clean(td.get_text()).upper():
            box = td.find_parent("table")
    if box is None:
        return out
    for tr in box.select("tr.decision"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) == 3:
            label = clean(tds[1].get_text(" ")).lower().replace(" ", "_")
            out[label] = [clean(tds[0].get_text()) or None, clean(tds[2].get_text()) or None]
    last = box.select("tr.bottom-row td")
    if len(last) == 2:
        out["nickname"] = [clean(td.get_text()) or None for td in last]
    return out


def parse_decision(html: str, decision_id: str) -> dict:
    """/decision/<id>/<slug> -> {decision, judge_rounds, judge_totals, media_scores, fan_scores}."""
    s = BeautifulSoup(html, "lxml")
    top = s.select_one("td.decision-top a[href*='fighter/']")
    bottom = s.select_one("td.decision-bottom a[href*='fighter/']")
    middle = s.select_one("td.decision-middle")
    result = clean(middle.get_text()).lower() if middle else None
    method_el = s.select_one("th.event2")
    event_cell = s.select_one("td.decision-top2 a[href*='event/']")
    event_id, event_name = entity_link(event_cell, "event")
    head = event_cell.find_parent("td") if event_cell is not None else None
    lines = [clean(x) for x in head.get_text("\n").split("\n") if clean(x)] if head is not None else []
    ref = s.select_one("td.decision-bottom2")
    referee = clean(ref.get_text()).removeprefix("REFEREE:").strip() if ref and "REFEREE" in ref.get_text() else None
    logo = LOGO.search(head.get("style", "")) if head is not None else None

    deductions = []
    for td in s.find_all("td"):
        text = clean(td.get_text(" "))
        m = DEDUCTION.fullmatch(text) if "deducted" in text and len(text) < 200 else None
        if m and not td.find("td"):
            deductions.append({"fighter": m.group(1), "points": int(m.group(2)), "round": int(m.group(3)),
                               "reason": m.group(4) or None})

    rounds, totals = [], []
    for slot, cell in enumerate(s.select("td.judge"), start=1):
        table = cell.find_parent("table")
        judge_id, judge_name = entity_link(cell.find("a"), "judge")
        judge_name = judge_name or clean(cell.get_text()) or None
        card = []
        for tr in table.select("tr.decision"):
            tds = [clean(td.get_text()) for td in tr.find_all("td", recursive=False)]
            if len(tds) == 3 and tds[0].isdigit():
                card.append({"decision_id": decision_id, "judge_slot": slot, "judge_id": judge_id,
                             "judge_name": judge_name, "round": int(tds[0]),
                             "score_1": to_int(tds[1]), "score_2": to_int(tds[2])})
        tot = [to_int(td.get_text()) for td in table.select("tr.bottom-row td.bottom-cell")]
        t1, t2 = (tot + [None, None])[:2]
        # A point score is never below 7, so a card whose every number is 0 or 1 is
        # the legend's code, its round rows too ("1 -" = favoured): not round scores.
        numbers = [v for r in card for v in (r["score_1"], r["score_2"])] + [t1, t2]
        kind = "score" if max((v for v in numbers if v is not None), default=0) > 1 else "legend"
        if kind == "legend":
            for r in card:
                r["score_1"] = r["score_2"] = None
        rounds += card
        totals.append({"decision_id": decision_id, "judge_slot": slot, "judge_id": judge_id, "judge_name": judge_name,
                       "total_1": t1, "total_2": t2, "totals_kind": kind,
                       "rounds_known": sum(r["score_1"] is not None for r in card), "rounds_listed": len(card)})

    media = []
    head_cell = next((td for td in s.select("td.top-cell") if "MEDIA SCORES" in td.get_text()), None)
    if head_cell is not None:
        for tr in head_cell.find_parent("table").select("tr.decision"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) != 3:
                continue
            outlet = tds[0].find("i")
            outlet_text = clean((outlet or tds[0]).get_text())
            # "Writer<br><i>Outlet</i>", or the outlet alone
            person = clean("".join(x for x in tds[0].find_all(string=True) if not x.find_parent("i"))) \
                if outlet is not None else None
            sc = clean(tds[1].get_text())
            m1, m2 = score_pair(sc)
            a = tds[1].find("a")
            media.append({"decision_id": decision_id, "writer": person or None, "outlet": outlet_text or None,
                          "score": sc or None, "score_1": m1, "score_2": m2,
                          "winner": clean(tds[2].get_text()) or None, "source_url": a.get("href") if a else None})

    fan, submitted, winner_counts = [], None, None
    box = s.find(id="scorecard_totals")
    if box is not None:
        for tr in box.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) == 4 and tds[3].get("class") == ["bar"]:
                sc = clean(tds[1].get_text())
                f1, f2 = score_pair(sc)
                fan.append({"decision_id": decision_id, "scope": "total", "round": None,
                            "outcome": clean(tds[0].get_text()), "score": sc, "score_hi": f1, "score_lo": f2,
                            "pct": pct(tds[2].get_text())})
        for cell in box.select("td.top-cell-small"):
            m = re.fullmatch(r"ROUND (\d+)", clean(cell.get_text()).upper())
            if not m:
                continue
            for tr in cell.find_parent("table").select("tr.decision"):
                tds = [clean(td.get_text()) for td in tr.find_all("td", recursive=False)]
                if len(tds) == 3:
                    f1, f2 = score_pair(tds[0])
                    fan.append({"decision_id": decision_id, "scope": "round", "round": int(m.group(1)),
                                "outcome": tds[1], "score": tds[0], "score_hi": f1, "score_lo": f2, "pct": pct(tds[2])})
    sub = s.find(id="scorecards_submitted")
    if sub is not None and sub.find("b"):
        submitted = to_int(sub.find("b").get_text())
    pie = PIE.search(html)
    if pie:
        # The page does not escape apostrophes in names: ['O'Malley', 1730]
        winner_counts = {clean(k): int(v) for k, v in re.findall(r"\[\s*'([^\[\]]*?)'\s*,\s*(\d+)\s*\]", pie.group(1))}
    for row in fan:
        row["est_count"] = round(row["pct"] * submitted / 100) if row["pct"] is not None and submitted else None

    decision = {"decision_id": decision_id, "event_id": event_id, "event_name": event_name,
                "event_date": parse_date(lines[1]) if len(lines) > 1 else None,
                "location": lines[2] if len(lines) > 2 else None, "promotion_logo": logo.group(1) if logo else None,
                "fighter_1": fighter_link(top), "fighter_2": fighter_link(bottom),
                "result": result,          # "defeats" or "drew with" (or "no contest" if the site ever says so)
                "method": clean(method_el.get_text()) if method_el else None, "referee": referee or None,
                "deductions": deductions, "tale_of_the_tape": _tape(s),
                "fan_submitted": submitted, "fan_winner_counts": winner_counts,
                "n_judges": len(totals), "n_media": len(media)}
    return {"decision": decision, "judge_rounds": rounds, "judge_totals": totals, "media_scores": media,
            "fan_scores": fan}


def parse_event(html: str, event_id: str) -> dict:
    """/event/<id>/<slug> -> one row: name, venue, location, date, promotion logo and
    each decision with its method and the judges' totals as listed."""
    s = BeautifulSoup(html, "lxml")
    head = s.select_one("td.decision-top2")
    lines = [clean(x) for x in head.get_text("\n").split("\n") if clean(x)] if head is not None else []
    logo = LOGO.search(head.get("style", "")) if head is not None else None
    date_el = s.select_one("td.decision-bottom2")
    decisions = []
    for a in s.select("a[href*='decision/']"):
        found = link(a.get("href"))
        if not found or found[0] != "decision":
            continue
        tr = a.find_parent("tr")
        tds = tr.find_all("td", recursive=False)
        judges = []
        for td in tds[1:]:
            j_id, j_name = entity_link(td.find("a"), "judge")
            sc = td.find("span")
            judges.append({"judge_id": j_id, "judge_name": j_name or clean(td.find("b").get_text() if td.find("b") else ""),
                           "score": clean(sc.get_text()) if sc else None})
        method = tds[0].find("i")
        decisions.append({"decision_id": found[1], "slug": found[2], "bout": clean(a.get_text()),
                          "method": clean(method.get_text()) if method else None, "judges": judges})
    return {"event_id": event_id, "event_name": lines[0] if lines else None,
            "venue": lines[1] if len(lines) > 2 else None, "location": lines[-1] if len(lines) > 1 else None,
            "event_date": parse_date(date_el.get_text()) if date_el else None,
            "promotion_logo": logo.group(1) if logo else None, "decisions": decisions}
