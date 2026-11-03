"""UFC Stats HTML -> records. Pure functions, no network: the crawler
(pipeline/ufcstats.py) and any replay of the raw archive call the same code.

Rows keep the column names of the 2026-02/03 export in
~/ufc-elo-calculator/seeder_data/data/ufcstats_*.csv, so a crawl partition
lines up with it. Two of those names are kept for that reason only:
`red_fighter_id` / `blue_fighter_id` are the first and second fighter as the
fight page lists them (UFC Stats lists the winner first, not the red corner),
and `winner_fighter_id` is an id, or "draw" / "nc".

A stat that UFC Stats shows as "--" or "---" (not recorded: early events,
control time before ~2000) is None, never 0.
"""
import re
from datetime import datetime

from bs4 import BeautifulSoup

ID = re.compile(r"/(?:event|fight|fighter)-details/([0-9a-f]{16})")
CHALLENGE_MARK = "Checking your browser"
RESULT_FLAG = {"win": "W", "loss": "L", "draw": "D", "nc": "NC"}


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def id_of(href: str | None) -> str | None:
    m = ID.search(href or "")
    return m.group(1) if m else None


def parse_date(text: str) -> str | None:
    """'October 24, 2026' or 'Oct. 31, 2026' -> '2026-10-24'."""
    t = clean(text).replace(".", "")
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(t, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def missing(v: str) -> bool:
    return v in ("", "--", "---")


def to_int(v: str) -> int | None:
    v = clean(v)
    return None if missing(v) else int(v)


def of(v: str) -> tuple[int | None, int | None, str]:
    """'24 of 47' -> (24, 47, '24 of 47')."""
    v = clean(v)
    m = re.fullmatch(r"(\d+) of (\d+)", v)
    return (int(m.group(1)), int(m.group(2)), v) if m else (None, None, v)


def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# ---------------------------------------------------------------- events list

def parse_events_list(html: str) -> list[dict]:
    """/statistics/events/completed[?page=all] -> one dict per event row, newest first.
    The top row is the NEXT event (dated in the future, no results yet)."""
    out = []
    for row in soup(html).select("tr.b-statistics__table-row, tr.b-statistics__table-row_type_first"):
        a = row.select_one("a[href*='event-details/']")
        if not a:
            continue
        cells = row.find_all("td")
        out.append({"ufcstats_event_id": id_of(a["href"]), "event_name": clean(a.get_text()),
                    "event_date": parse_date(row.select_one(".b-statistics__date").get_text()),
                    "location": clean(cells[1].get_text()) if len(cells) > 1 else None})
    return out


def parse_fighters_list(html: str) -> list[str]:
    """/statistics/fighters?char=<c>&page=all -> fighter ids, in listing order."""
    return list(dict.fromkeys(id_of(a["href"]) for a in soup(html).select("a[href*='fighter-details/']")))


# ---------------------------------------------------------------------- event

def parse_event(html: str, event_id: str) -> dict:
    """/event-details/<id> -> the event and its card. `completed` is True only when
    every bout on the card has a result flag; UFC Stats fills them in during the
    night, and bouts that fall through are removed from the card."""
    s = soup(html)
    info = {}
    for li in s.select("li.b-list__box-list-item"):
        label, _, value = clean(li.get_text()).partition(":")
        info[label.strip().lower()] = value.strip()
    fights = []
    for row in s.select("tr.b-fight-details__table-row[data-link]"):
        cells = row.find_all("td", recursive=False)
        flags = [clean(f.get_text()).lower() for f in cells[0].select(".b-flag__text")]
        fighters = [id_of(a.get("href")) for a in cells[1].select("a[href*='fighter-details/']")]
        fights.append({"ufcstats_fight_id": id_of(row["data-link"]), "fighter_ids": fighters,
                       "result_flags": flags,
                       "bonuses": [b.rsplit("/", 1)[-1].removesuffix(".png") for b in
                                   (img.get("src", "") for img in cells[6].find_all("img")) if b]})
    title = s.select_one("h2.b-content__title")
    return {"ufcstats_event_id": event_id,
            "ufcstats_event_url": f"http://ufcstats.com/event-details/{event_id}",
            "event_name": clean(title.get_text()) if title else None,
            "event_date": parse_date(info.get("date", "")),
            "location": info.get("location") or None,
            "fights": fights,
            "completed": bool(fights) and all(f["result_flags"] for f in fights)}


# ---------------------------------------------------------------------- fight

TOTALS_COLS = ("kd", "sig_str", "sig_str_pct", "total_str", "td", "td_pct", "sub_att", "rev", "ctrl")
SIG_COLS = ("sig_str", "sig_str_pct", "head", "body", "leg", "distance", "clinch", "ground")


def _table_rows(table) -> list[tuple[str | None, list[str], list[list[str]]]]:
    """A stats table -> [(round label or None, [fighter ids], [[cell lines]...])].
    Per-round tables put a 'Round N' header before each row."""
    out = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        ids = [id_of(a.get("href")) for a in tds[0].find_all("a")]
        cols = [[clean(p.get_text()) for p in td.find_all("p")] for td in tds[1:]]
        head = tr.find_previous(string=re.compile(r"^\s*Round \d+\s*$"))
        label = None
        if head and table in head.parents:
            label = re.search(r"\d+", head).group()
        out.append((label, ids, cols))
    return out


def _fighter_stats(ids: list, cols: list[list[str]], names: tuple[str, ...]) -> list[dict]:
    rows = []
    for side, fid in enumerate(ids):
        rows.append({"ufcstats_fighter_id": fid,
                     **{name: (col[side] if side < len(col) else "") for name, col in zip(names, cols)}})
    return rows


def _totals_row(fight_id: str, r: dict) -> dict:
    sig_l, sig_a, sig_raw = of(r["sig_str"])
    tot_l, tot_a, tot_raw = of(r["total_str"])
    td_l, td_a, td_raw = of(r["td"])
    return {"ufcstats_fight_id": fight_id, "ufcstats_fighter_id": r["ufcstats_fighter_id"],
            "kd": to_int(r["kd"]), "sig_str_land": sig_l, "sig_str_att": sig_a, "sig_str_raw": sig_raw,
            "total_str_land": tot_l, "total_str_att": tot_a, "total_str_raw": tot_raw,
            "td_land": td_l, "td_att": td_a, "td_raw": td_raw,
            "sub_att": to_int(r["sub_att"]), "rev": to_int(r["rev"]),
            "ctrl": None if missing(r["ctrl"]) else r["ctrl"]}


def _split3(fight_id: str, r: dict, rnd: str, keys: tuple[str, str, str]) -> dict:
    row = {"ufcstats_fight_id": fight_id, "ufcstats_fighter_id": r["ufcstats_fighter_id"], "round": rnd}
    for k in keys:
        land, att, raw = of(r[k])
        row.update({f"{k}_land": land, f"{k}_att": att, f"{k}_raw": raw})
    return row


def parse_judges(details: str) -> list[dict]:
    """'Tolly Plested 27 - 30. Ben Cartlidge 27 - 30. ...' -> [{judge, scores}], scores
    in the order printed. UFC Stats does not say whose score comes first.

    A technical decision opens with a reason before the first judge ('Technical
    Decision after Headbutt by Abdul-Malik Will Fisher 18 - 20.'), so that segment
    keeps only its last two words. method_detail always keeps the full text."""
    out = []
    for i, (name, a, b) in enumerate(re.findall(r"([^.\d][^\d]*?)\s+(\d+)\s*-\s*(\d+)\.?", details)):
        words = clean(name).split()
        if i == 0 and len(words) > 4:
            words = words[-2:]
        out.append({"judge": " ".join(words), "scores": [int(a), int(b)]})
    return out


def parse_fight(html: str, fight_id: str) -> dict:
    """/fight-details/<id> -> {fight, totals, rounds, sig_by_target, sig_by_position}.
    The sig-strike rows carry round 'totals' for the whole fight, then '1', '2', ...
    as the export does."""
    s = soup(html)
    persons = s.select("div.b-fight-details__person")
    fighters = [id_of(p.select_one("a[href*='fighter-details/']").get("href")) for p in persons]
    results = [clean(p.select_one(".b-fight-details__person-status").get_text()) for p in persons]
    nicknames = [clean(p.select_one(".b-fight-details__person-title").get_text()).strip('"') or None
                 for p in persons]

    box = s.select_one("div.b-fight-details__fight")
    title_el = box.select_one(".b-fight-details__fight-title")
    title = clean(title_el.get_text())
    bonuses = [img.get("src", "").rsplit("/", 1)[-1].removesuffix(".png") for img in title_el.find_all("img")]
    fields = {}
    for item in box.select("i.b-fight-details__text-item, i.b-fight-details__text-item_first"):
        label = item.select_one(".b-fight-details__label")
        if label:
            key = clean(label.get_text()).rstrip(":").lower()
            fields[key] = clean(item.get_text()).removeprefix(clean(label.get_text())).strip()
    details = ""
    for p in box.select("p.b-fight-details__text"):
        label = p.select_one(".b-fight-details__label")
        if label and clean(label.get_text()).lower().startswith("details"):
            details = clean(p.get_text()).removeprefix(clean(label.get_text())).strip()

    fmt = fields.get("time format", "")
    sched = re.match(r"(\d+) Rnd", fmt)
    if results.count("W") == 1:
        winner = fighters[results.index("W")]
    elif results and all(r == "D" for r in results):
        winner = "draw"
    elif results and all(r == "NC" for r in results):
        winner = "nc"
    else:
        winner = None
    method = fields.get("method") or None
    fight = {"ufcstats_fight_id": fight_id, "ufcstats_fight_url": f"http://ufcstats.com/fight-details/{fight_id}",
             "ufcstats_event_id": id_of((s.select_one("h2.b-content__title a") or {}).get("href")),
             "red_fighter_id": fighters[0] if fighters else None,
             "blue_fighter_id": fighters[1] if len(fighters) > 1 else None,
             "weight_class": title, "gender": "F" if "Women's" in title else "M",
             "is_title_bout": "Title Bout" in title,
             "scheduled_rounds": int(sched.group(1)) if sched else None,
             "winner_fighter_id": winner, "method": method, "method_detail": details or None,
             "round": to_int(fields.get("round", "")), "time": fields.get("time") or None,
             "referee": fields.get("referee") or None,
             # beyond the export's columns
             "time_format": fmt or None, "results": results, "nicknames": nicknames, "bonuses": bonuses,
             "judges": parse_judges(details) if method and method.startswith("Decision") else []}

    tables = s.select("table")
    totals, rounds, target, position = [], [], [], []
    stats_tables = [t for t in tables if t.find("td")]
    for t in stats_tables:
        heads = [clean(th.get_text()).lower() for th in t.select("thead th")]
        is_sig = "head" in heads
        for label, ids, cols in _table_rows(t):
            for r in _fighter_stats(ids, cols, SIG_COLS if is_sig else TOTALS_COLS):
                if is_sig:
                    rnd = label or "totals"
                    target.append(_split3(fight_id, r, rnd, ("head", "body", "leg")))
                    position.append(_split3(fight_id, r, rnd, ("distance", "clinch", "ground")))
                else:
                    row = _totals_row(fight_id, r)
                    if label is None:
                        totals.append(row)
                    else:
                        rounds.append({**row, "round": int(label)})
    return {"fight": fight, "totals": totals, "rounds": rounds,
            "sig_by_target": target, "sig_by_position": position}


# -------------------------------------------------------------------- fighter

def _inches(text: str) -> float | None:
    m = re.fullmatch(r"(\d+)' (\d+)\"", clean(text))
    if m:
        return float(int(m.group(1)) * 12 + int(m.group(2)))
    m = re.fullmatch(r"([\d.]+)\"", clean(text))
    return float(m.group(1)) if m else None


def parse_fighter(html: str, fighter_id: str) -> dict:
    """/fighter-details/<id> -> the export's fighter columns, plus the career stats box."""
    s = soup(html)
    info = {}
    for li in s.select("li.b-list__box-list-item"):
        label, _, value = clean(li.get_text()).partition(":")
        if label:
            info[label.strip().lower()] = value.strip()
    name = s.select_one(".b-content__title-highlight")
    record = s.select_one(".b-content__title-record")
    nick = s.select_one(".b-content__Nickname")
    dob = info.get("dob", "")
    return {"ufcstats_fighter_id": fighter_id, "ufcstats_url": f"http://ufcstats.com/fighter-details/{fighter_id}",
            "name": clean(name.get_text()) if name else None,
            "record": clean(record.get_text()).removeprefix("Record:").strip() if record else None,
            "dob": None if missing(dob) else parse_date(dob),
            "height_in": _inches(info.get("height", "")), "reach_in": _inches(info.get("reach", "")),
            "stance": info.get("stance") or None,
            # beyond the export's columns
            "nickname": clean(nick.get_text()) or None if nick else None,
            "weight": None if missing(info.get("weight", "")) else info.get("weight"),
            "career": {k: v for k, v in info.items()
                       if k in ("slpm", "str. acc.", "sapm", "str. def", "td avg.", "td acc.", "td def.", "sub. avg.")},
            # every fight UFC Stats has for them, upcoming included: the only path to
            # events the completed-events list leaves out (DWCS, PRIDE, UFC 1, ...)
            "fights": [{"ufcstats_fight_id": id_of(row.get("data-link")),
                        "ufcstats_event_id": id_of((row.select_one("a[href*='event-details/']") or {}).get("href"))}
                       for row in s.select("tr.b-fight-details__table-row[data-link]")]}
