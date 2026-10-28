#!/usr/bin/env python3
"""Stage-3 extractors: Tapology event page + bout page -> plain dicts.

Capture-everything policy (see docs/field-census.md): every label/value pair on the
page is kept under its RAW label as well as a canonical key, so a Tapology label we
have never seen still lands in the output instead of being dropped.

Layout note that drives the whole file: Tapology renders desktop AND mobile copies of
most values into the same HTML. The mobile copy always carries the class `md:hidden`.
Dropping those elements once, up front, removes every duplicate site-wide -- in the
production block, in the fighter-comparison tables and in the bout rows.
"""
import json, re, sys
from pathlib import Path
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- helpers ----
MISSING = {"", "-", "--", "n/a", "N/A", "None", "none", "TBD", "tbd"}
ICONS = {"add_circle", "grid_view", "format_list_bulleted", "blur_on", "scale",
         "swipe_right", "forum", "close", "smartphone", "pageview", "menu",
         "devices", "expand_more", "expand_less"}

def sq(t):
    return re.sub(r"\s+", " ", t or "").strip()

def val(t):
    """Normalise a scraped value; empty/placeholder -> None."""
    t = sq(t)
    return None if t in MISSING else t

def txt(el, sep=" "):
    return sq(el.get_text(sep, strip=True)) if el else None

def soup_of(html):
    s = BeautifulSoup(html, "lxml")
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    for el in list(s.find_all(class_="md:hidden")):      # mobile duplicates
        if el.parent is not None:
            el.decompose()
    return s

def link_platform(url):
    h = (url or "").lower()
    for k in ("facebook", "instagram", "youtube", "tiktok", "wikipedia", "sherdog",
              "twitter", "x.com", "combatreg", "ufcstats", "bestfightodds", "espn",
              "mixedmartialarts", "bloodyelbow"):
        if k in h:
            return "twitter" if k == "x.com" else k
    return "official/other"

def id_slug(href, kind):
    """/fightcenter/fighters/40148-islam-makhachev -> (40148, '40148-islam-makhachev').
    Legacy records have no numeric id (/fightcenter/fighters/a-sol-kwan) -> (None, slug)."""
    if not href:
        return None, None
    m = re.search(rf"/{kind}/([^/?#]+)", href)
    if not m:
        return None, None
    slug = m.group(1)
    mid = re.match(r"^(\d+)-", slug)
    return (int(mid.group(1)) if mid else None), slug

def fighter_slug(href):
    """Whatever follows /fighters/ IS the identity. There is no fighter id -- some slugs
    happen to start with digits ("40148-islam-makhachev", "134267"), others never do
    ("a-sol-kwan"), and neither form is more canonical than the other."""
    if not href:
        return None
    m = re.search(r"/fighters/([^/?#]+)", href)
    return m.group(1) if m else None

def canon(label):
    """Raw Tapology label -> stable snake_case key."""
    k = sq(label).rstrip(":").lower()
    k = k.replace("&", "and")
    k = re.sub(r"[^\w\s]", " ", k)
    return re.sub(r"\s+", "_", k.strip())

def label_value_list(container):
    """Every `li` of the form <span.font-bold>Label:</span><span>Value</span>.
    Returns [(raw_label, value_text, li_element)] in document order, first-wins."""
    out, seen = [], set()
    if not container:
        return out
    for li in container.select("li"):
        lab = li.find("span", class_="font-bold")
        if not lab:
            continue
        raw = sq(lab.get_text())
        if not raw.endswith(":") or raw in seen:
            continue
        seen.add(raw)
        rest = li.__copy__()
        first = rest.find("span", class_="font-bold")
        if first:
            first.decompose()
        out.append((raw, val(rest.get_text(" ", strip=True)), li))
    return out

def links_in(li):
    out = []
    if not li:
        return out
    for a in li.select("a[href]"):
        href = a.get("href")
        if not href or not href.startswith("http"):
            continue
        out.append({"platform": link_platform(href), "url": href,
                    "tag": (re.search(r"external_click_(\w+)", a.get("onclick") or "")
                            or [None, None])[1]})
    return out

def section_after(soup, heading_text):
    """Sibling blocks following an <h3>/<h4> whose text starts with heading_text,
    stopping at the next heading -- including one nested inside a sibling wrapper."""
    for el in soup.select("h1,h2,h3,h4,h5"):
        if sq(el.get_text()).startswith(heading_text):
            out = []
            for sib in el.next_siblings:
                if getattr(sib, "name", None) is None:
                    continue
                if sib.name in ("h1", "h2", "h3", "h4", "h5"):
                    break
                if sib.find(["h1", "h2", "h3", "h4", "h5"]):
                    break
                out.append(sib)
            return out
    return []

def comparison_table(table):
    """Fighter-comparison table -> {canon_key: {label, left, right}}.
    Rows are <td>left</td><td/><td>LABEL</td><td/><td>right</td>; spacer cells are
    empty. The label cell is found by position (middle), never by a fixed index, so
    rows with extra cells still align."""
    out = {}
    if not table:
        return out
    for tr in table.select("tr"):
        tds = tr.find_all("td", recursive=False) or tr.find_all("td")
        cells = [sq(td.get_text(" ", strip=True)) for td in tds]
        if len(cells) < 3:
            continue
        # label = the middle non-empty cell; spacers are the empty ones
        idx = [i for i, c in enumerate(cells) if c]
        if len(idx) < 1:
            continue
        mid = len(cells) // 2
        cand = min(idx, key=lambda i: (abs(i - mid), i))
        label = cells[cand]
        if not label:
            continue
        left = next((cells[i] for i in range(cand - 1, -1, -1) if cells[i]), "")
        right = next((cells[i] for i in range(cand + 1, len(cells)) if cells[i]), "")
        out[canon(label)] = {"label": label, "left": val(left), "right": val(right)}
    return out

# ------------------------------------------------------------ event page ----
RESULT_RE = re.compile(r"^(W|L|D|NC)$")
DUR_RE = re.compile(r"(\d+\s*Rounds?|\d+:\d{2}|Total|R\d)", re.I)
METHOD_RE = re.compile(r"(decision|ko|tko|submission|draw|no contest|dq|"
                       r"disqualification|overturned|technical|forfeit|retirement)", re.I)

def parse_bout_row(block, expanded):
    """One bout on an event page: #boutFullsize{id} plus its expanded details."""
    bid = int(re.sub(r"\D", "", block.get("id") or "0")) or None
    b = {"bout_id": bid, "bout_slug": None, "bout_url": None, "order": None,
         "billing": None, "weight_badge": None, "round_format": None, "sport": None,
         "title_bout": None, "method": None, "duration": None,
         "header_lines": [], "fighters": [], "comparison": {}}

    for a in block.select('a[href*="/fightcenter/bouts/"]'):
        _id, slug = id_slug(a["href"], "bouts")
        b["bout_slug"] = slug
        b["bout_url"] = "https://www.tapology.com" + a["href"].split("?")[0]
        if _id:
            b["bout_id"] = _id
        break

    # Header strip = FIRST child div of the bout block, holding up to three spans in a
    # fixed order: [title line, method, duration]. Anchor on position, never on the
    # colour class -- Tapology colours this strip per sport (text-tap_darkgold for MMA,
    # other palettes for karate/kickboxing/grappling), so a colour selector silently
    # drops every non-MMA bout.
    kids = block.find_all("div", recursive=False)
    # The header strip only exists when there is something to show. On a bout with no
    # recorded result it is omitted entirely and the fighter row becomes the first child
    # -- taking kids[0] blindly then reads "Main Event" as the method and the weight
    # pill as the title. The fighter row is the one carrying fighter links.
    row_i = next((i for i, k in enumerate(kids)
                  if k.select('a[href*="/fightcenter/fighters/"]')), None)
    head = kids[0] if (row_i is not None and row_i > 0) else None
    if head is not None:
        # Each of the three slots carries a stable class signature, and slots are simply
        # absent when empty (wrestling cards have no duration line). Classify by class,
        # not by span count or by method keywords -- "Pinfall"/"Tech Fall" are methods
        # too, and counting spans mislabels them as the title.
        spans = head.find_all("span")
        b["header_lines"] = [sq(sp.get_text(" ", strip=True)) for sp in spans
                             if sq(sp.get_text()) and sq(sp.get_text()) not in ICONS]
        for sp in spans:
            cls = " ".join(sp.get("class") or [])
            t = val(sp.get_text(" ", strip=True))
            if not t:
                continue
            if "uppercase" in cls:
                b["method"] = t
            elif "leading-relaxed" in cls or "leading-loose" in cls:
                b["duration"] = t
            else:
                b["title_bout"] = b["title_bout"] or t

    # The header's first line is an annotation, not necessarily a title: grappling cards
    # put the bracket stage there ("Openweight Finals"). Classify it, because "fetch the
    # bout page for title fights" would otherwise pull in every tournament round.
    if b["title_bout"]:
        t = b["title_bout"]
        # tournament FIRST: "Tournament Championship" is a bracket final, not a belt --
        # the 1960 Olympics alone supplies 49 of them. Plurals matter too: boxing
        # unification bouts read "... Middleweight Titles".
        if re.search(r"\b(tournament|final|finals|semifinal|quarterfinal|round of|"
                     r"bracket|opening round|reserve|grand prix|gp )\b", t, re.I):
            b["title_kind"] = "tournament"
        elif re.search(r"\b(titles?|championships?|champion|belt|medal match)\b", t, re.I):
            b["title_kind"] = "title"
        else:
            b["title_kind"] = "other"

    order = block.find(id=re.compile(r"^(boutCompactNumber|expandCollapse)"))
    if order is None:
        hashmark = block.find("span", string=re.compile(r"^\s*#\s*$"))
        order = hashmark.parent if hashmark else None
    if order is not None:
        m = re.search(r"#?\s*(\d+)", sq(order.get_text(" ", strip=True)))
        if m:
            b["order"] = int(m.group(1))
    billing = block.find("a", href=re.compile(r"/fightcenter/bouts/"))
    if billing:
        b["billing"] = val(billing.get_text(strip=True))
    # weight pill: first bg-* span holding an actual weight. The bg-* colour differs per
    # sport (bg-tap_darkgold MMA / bg-red-700 karate / bg-sky-700 kickboxing) so it is
    # kept as a signal rather than used as the selector.
    # NOTE: bs4 hands a class *string* to a callable filter, not a list -- match with a
    # regex on that string, then read the real class list off the element.
    for badge in block.find_all("span", class_=re.compile(r"(^|\s)bg-")):
        t = val(badge.get_text(strip=True))
        # Pills are usually a numeric limit ("170") but open/heavyweight bouts use a
        # class abbreviation instead ("HW", "OW", "SHW"), which a numeric-only filter
        # silently discards. weight_badge_kind distinguishes the two.
        if t and re.match(r"^[\d.]+$|^catch|^(HW|SHW|OW|LHW|MW|WW|LW|FW|BW|FLW|SW|CW)$",
                          t, re.I):
            b["weight_badge"] = t
            b["weight_badge_kind"] = "numeric" if re.match(r"^[\d.]+$", t) else "class"
            b["badge_class"] = next((x for x in badge.get("class") if x.startswith("bg-")), None)
            break
    # round format sits in a leaf div, sometimes alone ("5 x 5"), sometimes sharing the
    # line with a pro/am flag using a literal pipe ("3 x 3 | Pro").
    # Round format has several notations. "N x N" is the common one, but combat-sport
    # cards also use "3-3-3 +OT", "5 min +OT", "5-5 +OT", "2-2-2". Two-segment dash forms
    # are NOT accepted on their own -- "0-1" is a fighter record, not a format -- so the
    # dash form needs 3+ segments or an explicit "+OT".
    FMT_PATTERNS = (
        r"\b(\d+\s*x\s*\d+)\b",                        # 5 x 5
        r"\b(\d+(?:-\d+){2,}(?:\s*\+\s*OT)?)\b",         # 3-3-3, 3-3-3 +OT
        r"\b(\d+(?:-\d+)*\s*\+\s*OT)\b",                # 5-5 +OT
        r"\b(\d+\s*min(?:utes)?(?:\s*\+\s*OT)?)\b",     # 5 min +OT
    )
    for d in block.find_all(["div", "span"]):
        if d.find(["div", "span"]):
            continue                      # leaf elements only
        if d.find_parent(id=lambda i: i and "Bio" in i):
            continue                      # fighter bios hold records, not formats
        t = sq(d.get_text())
        for pat in FMT_PATTERNS:
            m = re.search(pat, t, re.I)
            if m:
                b["round_format"] = m.group(1)
                pa = re.search(r"\b(Pro|Am|Amateur)\b", t)
                b["pro_am_flag"] = pa.group(1) if pa else None
                break
        if b["round_format"]:
            break
    if b["duration"]:
        m = re.match(r"^(\d+:\d{2})\s+Round\s+(\d+)(?:\s+of\s+(\d+))?", b["duration"])
        if m:
            b["finish"] = True
            b["finish_time"], b["finish_round"] = m.group(1), int(m.group(2))
            b["scheduled_rounds"] = int(m.group(3)) if m.group(3) else None
        elif re.match(r"^\d+\s+Rounds?,", b["duration"]):
            b["finish"] = False

    if expanded:
        sport = expanded.find("div", class_=lambda c: c and "rounded-t-3xl" in c)
        b["sport"] = val(sport.get_text(" ", strip=True)) if sport else None
        if b["sport"]:
            t = b["sport"]
            # Age grade is a LEVEL, not a sport: Tapology counts "Juniors MMA" and
            # "Kids MMA" under "MMA Bouts: N" in the production block, so they must
            # collapse to the same sport_base or the card/declared counts disagree.
            LEVELS = r"(Professional|Amateur|Juniors|Junior|Kids|Youth)"
            m = re.match(rf"^{LEVELS}\s+", t)
            b["level"] = m.group(1) if m else None
            t = re.sub(rf"^{LEVELS}\s+", "", t)
            b["modified_rules"] = bool(re.search(r"\(Modified Rules\)", t))
            t = re.sub(r"\s*\(Modified Rules\)\s*", "", t).strip()
            m2 = re.match(rf"^{LEVELS}\s+", t)          # e.g. "Amateur Juniors MMA"
            if m2:
                b["age_grade"] = m2.group(1)
                t = re.sub(rf"^{LEVELS}\s+", "", t)
            b["sport_base"] = t or None
        b["comparison"] = comparison_table(expanded.find("table"))
        tbl = expanded.find("table")
        if tbl is not None:
            clone = expanded.__copy__()
            for t in clone.find_all("table"):
                t.decompose()
            extra = sq(clone.get_text(" ", strip=True))
            extra = re.sub(r"^(Professional|Amateur)[^:]*?(?=[A-Z][a-z]+:|$)", "", extra).strip()
            b["bout_notes"] = val(re.sub(r"\s*Matchup Page\s*$", "", extra))

    for side, key in ((0, "_leftBio"), (1, "_rightBio")):
        bio = block.find(id=re.compile(re.escape(key) + "$"))
        bio = block.find(id=f"boutFullsize{bid}{key}") or bio
        f = {"side": side, "name": None, "fighter_slug": None,
             "result": None, "record_after": None, "headshot_url": None,
             "nationality_code": None}
        if bio:
            a = bio.find("a", href=re.compile(r"/fightcenter/fighters/"))
            t = sq(bio.get_text(" ", strip=True))
            if a:
                f["name"] = val(a.get_text(strip=True))
                f["fighter_slug"] = fighter_slug(a["href"])
                f["fighter_linked"] = True
            else:
                # Fighters with no Tapology profile are printed as plain text, not a
                # link. The name is still there -- recover it by stripping the result
                # marker and the record phrase that share the same block.
                f["fighter_linked"] = False
                nm = re.sub(r"^(W|L|D|NC)\b\s*", "", t)
                nm = re.sub(r"\s*(Up to|Down to|Even at|Now)\s+[\d\-\u2013].*$", "", nm)
                f["name"] = val(nm)
            # The result marker is its own bold span (W green / L red / D blue /
            # N neutral). Read the span rather than regexing the text: Tapology writes
            # no-contest as a bare "N", which the old ^(W|L|D|NC) pattern missed
            # entirely -- silently dropping the result on every no-contest bout.
            marker = None
            for sp in bio.find_all("span"):
                st = sq(sp.get_text())
                if st in ("W", "L", "D", "N", "NC"):
                    marker = st
                    break
            if marker is None:                       # fallback for odd layouts
                mm = re.match(r"^(W|L|D|NC|N)\b", t)
                marker = mm.group(1) if mm else None
            f["result"] = "NC" if marker == "N" else marker
            m = re.search(r"(Up to|Down to|Even at|Now)\s+([\d\-–]+)", t)
            if m:
                f["record_after"] = m.group(2)
                f["record_after_phrase"] = m.group(0)
        img = block.find(id=re.compile(rf"^fighterBoutImage\d+$"))
        imgs = block.select('[id^="fighterBoutImage"] img')
        if len(imgs) > side:
            f["headshot_url"] = imgs[side].get("src")
        flags = [i for i in block.select("img[src*='/assets/flags/']")]
        if len(flags) > side:
            m = re.search(r"/flags/([A-Z]{2})-", flags[side].get("src") or "")
            f["nationality_code"] = m.group(1) if m else None
        b["fighters"].append(f)
    return b

def parse_event(html, url=None):
    s = soup_of(html)
    ev = {"event_url": url, "event_id": None, "event_slug": None, "event_name": None,
          "status": None, "sport_label": None, "date_header": None,
          "location_header": None, "region_slug": None,
          "promotion": {}, "poster_url": None,
          "production": {}, "production_raw": {}, "bout_counts": {},
          "promotion_links": [], "event_links": [],
          "bouts": [], "cancelled_bouts": [], "weigh_ins": [], "awards": [],
          "referees": []}
    if url:
        ev["event_id"], ev["event_slug"] = id_slug(url, "events")

    h2 = s.find("h2")
    ev["event_name"] = val(h2.get_text(" ", strip=True)) if h2 else None
    header = s.find(id="eventPageHeader")
    if header:
        ht = [sq(x) for x in header.stripped_strings]
        ht = [x for x in ht if x and x not in ICONS]
        # header reads [status, sport label, date, promotion, location]; status is an
        # open vocabulary ("Completed", "In Progress", "Upcoming", ...) so take it by
        # position and only reject strings that are clearly one of the other fields.
        STATUSES = ("Completed", "In Progress", "Upcoming", "Cancelled", "Postponed")
        merged = []
        for t in ht:                      # "Completed Muay Thai Event" -> status + label
            hit = next((st for st in STATUSES if t.startswith(st) and t != st), None)
            if hit and t.endswith("Event"):
                merged += [hit, t[len(hit):].strip()]
            else:
                merged.append(t)
        ht = merged
        for t in ht:
            if re.match(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$", t) and not ev["date_header"]:
                ev["date_header"] = t
            elif re.search(r"\bEvent$", t) and not ev["sport_label"]:
                ev["sport_label"] = t
            elif not ev["status"] and not ev["sport_label"] and not ev["date_header"]:
                ev["status"] = t
        a = header.find("a", href=re.compile(r"/fightcenter/promotions/"))
        if a:
            pid, pslug = id_slug(a["href"], "promotions")
            ev["promotion"] = {"promotion_id": pid, "promotion_slug": pslug,
                               "name": val(a.get_text(strip=True)), "logo_url": None}
        r = header.find("a", href=re.compile(r"^/regions/"))
        if r:
            ev["location_header"] = val(r.get_text(strip=True))
            ev["region_slug"] = r["href"].replace("/regions/", "")

    # Scope the logo to the promotion icon / header. Some events have NO promotion at
    # all, and an unscoped search grabs the first /logo_squares/ image anywhere on the
    # page -- a sidebar logo from an unrelated promotion, silently mis-attributed.
    logo = None
    for sel in ("#eventPageDesktopPromotionIcon", "#eventPageMobilePromotionIcon",
                "#eventPageHeader"):
        holder = s.select_one(sel)
        if holder is not None:
            logo = holder.find("img", src=re.compile(r"/logo_squares/"))
            if logo is not None:
                break
    if logo is not None:
        ev.setdefault("promotion", {})
        ev["promotion"]["logo_url"] = logo.get("src")
        if not ev["promotion"].get("name"):
            ev["promotion"]["name"] = logo.get("alt")
    poster = s.find("img", src=re.compile(r"/poster_images/"))
    if poster is not None:
        ev["poster_url"] = poster.get("src")

    pd = s.find(id="primaryDetailsContainer")
    for raw, v, li in label_value_list(pd):
        ev["production_raw"][raw] = v
        key = canon(raw)
        ev["production"][key] = v
        m = re.match(r"^(.*?)\s+Bouts:$", raw)
        if m:
            try:
                ev["bout_counts"][m.group(1)] = int(re.sub(r"\D", "", v or ""))
            except ValueError:
                ev["bout_counts"][m.group(1)] = v
        if raw == "Promotion Links:":
            ev["promotion_links"] = links_in(li)
        elif raw == "Event Links:":
            ev["event_links"] = links_in(li)
        elif raw == "Promotion:":
            a = li.find("a", href=re.compile(r"/fightcenter/promotions/"))
            if a and not ev["promotion"].get("promotion_id"):
                pid, pslug = id_slug(a["href"], "promotions")
                ev["promotion"].update({"promotion_id": pid, "promotion_slug": pslug,
                                        "name": val(a.get_text(strip=True))})
        elif raw == "Location:":
            a = li.find("a", href=re.compile(r"^/regions/"))
            if a:                       # some locations carry no region link at all
                ev["region_slug"] = a["href"].replace("/regions/", "")
            ev["location_header"] = ev["location_header"] or v

    card = s.find(id="sectionFightCard") or s
    if card is not s:
        note = sq(card.get_text(" ", strip=True))
        if re.search(r"No fight card available", note, re.I):
            ev["card_note"] = "no_fight_card_available"
        elif re.search(r"This event was cancell?ed", note, re.I):
            ev["card_note"] = "event_cancelled"
    for block in card.select('[id^="boutFullsize"]'):
        if not re.match(r"^boutFullsize\d+$", block.get("id") or ""):
            continue
        bid = re.sub(r"\D", "", block["id"])
        expanded = card.find(id=f"boutExpandedDetails{bid}")
        if expanded is None or expanded.find_parent(id=block["id"]) is None:
            expanded = block.find(id=f"boutExpandedDetails{bid}")
        row = parse_bout_row(block, expanded)
        # some cards carry no bout number at all (IBJJF brackets); keep the position on
        # the page so every bout is orderable regardless.
        row["card_position"] = len(ev["bouts"]) + 1
        ev["bouts"].append(row)

    # Organisation: a single grouping key for "who ran this". Tapology sets
    # "Promotion: N/A" on some events (e.g. WAKO World Championships 2013) while still
    # naming the body under "Ownership:", so promotion alone loses those entirely.
    # Ownership is NOT a promotion synonym -- UFC events read Promotion: UFC +
    # Ownership: Zuffa, LLC -- so it is only a fallback, and the source is recorded.
    _prom = (ev.get("promotion") or {}).get("name")
    _own = ev["production"].get("ownership")
    if _prom:
        ev["organisation"], ev["organisation_source"] = _prom, "promotion"
    elif _own:
        ev["organisation"], ev["organisation_source"] = _own, "ownership"
    else:
        ev["organisation"], ev["organisation_source"] = None, None
    ev["ownership"] = _own          # surfaced at top level: corporate lineage across
                                    # rebrands (Zuffa -> UFC/WEC/Strikeforce)

    # Tapology's `order` is the slot on the ANNOUNCED card, so it can exceed the number
    # of bouts actually shown (removed/cancelled bouts leave gaps). card_position is the
    # position among displayed bouts. Where both exist they agree 93.3% of the time, so
    # the derived value is a good fallback -- but it is kept as a SEPARATE field, and
    # order_source records which one order_effective came from.
    _n = len(ev["bouts"])
    for _b in ev["bouts"]:
        _b["order_derived"] = _n - _b["card_position"] + 1
        _b["order_effective"] = _b["order"] if _b["order"] is not None else _b["order_derived"]
        _b["order_source"] = "page" if _b["order"] is not None else "derived"

    # Not every page carries "<Sport> Bouts: N" in the production block (Karate Combat
    # has none). Fall back to counting the card by each bout's own sport banner, and
    # record which way the number was obtained.
    # Per-sport counts ALWAYS come from the card. The production block's "<Sport> Bouts:"
    # labels only cover the sports Tapology tracks with counters, so on a mixed card they
    # under-report badly (Karate Combat 62: "Grappling Bouts: 2" for a 12-bout card that is
    # 10 karate + 2 grappling). The declared numbers are kept for cross-checking.
    from collections import Counter
    ev["bout_counts_declared"] = dict(ev["bout_counts"])
    # Group by sport_base so age grades (Kids/Juniors) and rule variants fold into their
    # parent sport, matching how Tapology's own "<Sport> Bouts: N" counters work.
    # bout_counts_raw keeps the verbatim banners for anyone who needs the distinction.
    ev["bout_counts"] = dict(Counter(
        b.get("sport_base") or re.sub(r"^(Professional|Amateur)\s+", "",
                                      b["sport"] or "Unknown")
        for b in ev["bouts"]))
    ev["bout_counts_raw"] = dict(Counter(b["sport"] or "Unknown" for b in ev["bouts"]))
    ev["bout_counts_source"] = "counted_from_card"
    ev["bout_counts_base"] = dict(Counter(
        b.get("sport_base") or "Unknown" for b in ev["bouts"]))
    ev["bout_counts_agree"] = (not ev["bout_counts_declared"]) or all(
        ev["bout_counts_base"].get(k) == v
        for k, v in ev["bout_counts_declared"].items())
    ev["bout_sports"] = sorted({b["sport"] for b in ev["bouts"] if b["sport"]})
    ev["pro_am"] = sorted({("Amateur" if (b["sport"] or "").startswith("Amateur") else "Professional")
                           for b in ev["bouts"] if b["sport"]})

    for cb in s.select('[id^="bout"][id$="Cancelled"]'):
        a = cb.find("a", href=re.compile(r"/fightcenter/bouts/"))
        bid, bslug = id_slug(a["href"], "bouts") if a else (None, None)
        fighters = []
        for fa in cb.select('a[href*="/fightcenter/fighters/"]'):
            item = {"name": val(fa.get_text(strip=True)),
                    "fighter_slug": fighter_slug(fa["href"])}
            if item not in fighters:
                fighters.append(item)
        parts = [p for p in (sq(x) for x in cb.stripped_strings) if p]
        ev["cancelled_bouts"].append({
            "bout_id": bid or int(re.sub(r"\D", "", cb["id"]) or 0) or None,
            "bout_slug": bslug, "status": val(a.get_text(strip=True)) if a else None,
            "reason": next((p for p in parts if p not in
                            [f["name"] for f in fighters] + ["cancelled", "fizzled"]), None),
            "fighters": fighters, "raw_text": " | ".join(parts)})

    # --- weigh-ins: rows are `div.border-dotted`; order span, then two 47% halves
    for blk in section_after(s, "Weigh-In Results"):
        for row in blk.select("div.border-dotted"):
            order = row.find("span")
            halves = row.select("div[class*='basis-[47%]']")
            raw = sq(row.get_text(" ", strip=True))
            rec = {"order": int(sq(order.get_text())) if order and sq(order.get_text()).isdigit() else None,
                   "left_name": None, "left_weight": None,
                   "right_name": None, "right_weight": None, "raw_text": raw}
            if len(halves) >= 2:
                l = sq(halves[0].get_text(" ", strip=True))
                r = sq(halves[1].get_text(" ", strip=True))
                m = re.match(r"^(.*?)\s+([\d.]+|N/A|-)$", l)      # "Name 170.0"
                if m:
                    rec["left_name"], rec["left_weight"] = val(m.group(1)), val(m.group(2))
                m = re.match(r"^([\d.]+|N/A|-)\s+(.*)$", r)        # "170.0 Name"
                if m:
                    rec["right_weight"], rec["right_name"] = val(m.group(1)), val(m.group(2))
            ev["weigh_ins"].append(rec)
        if not ev["weigh_ins"]:
            ev["weigh_ins_raw"] = txt(blk, " | ")

    # --- awards: alternating <span.text-tap_darkgold>AWARD</span> + <div>bout • detail</div>
    for blk in section_after(s, "Awards & Bonuses"):
        for sp in blk.find_all("span", class_=lambda c: c and "text-tap_darkgold" in c):
            body = sp.find_next_sibling("div")
            parts = [sq(x) for x in body.stripped_strings if sq(x) not in ("\u2022", "")] if body else []
            ev["awards"].append({"award": sq(sp.get_text()),
                                 "bout": parts[0] if parts else None,
                                 "detail": " ".join(parts[1:]) or None})
        if not ev["awards"]:
            ev["awards_raw"] = txt(blk, " | ")

    # --- referees: rows are `div.border-dotted`; matchup link, then referee span
    for blk in section_after(s, "Fight Referees"):
        for row in blk.select("div.border-dotted"):
            a = row.find("a", href=re.compile(r"/fightcenter/bouts/"))
            bid, bslug = id_slug(a["href"], "bouts") if a else (None, None)
            ref = row.find("span", class_=lambda c: c and "text-neutral-800" in c)
            order = row.find("span", class_=lambda c: c and "font-bold" in c)
            ev["referees"].append({
                "bout_id": bid, "bout_slug": bslug,
                "order": int(sq(order.get_text())) if order and sq(order.get_text()).isdigit() else None,
                "matchup": val(a.get_text(strip=True).rstrip(":")) if a else None,
                "referee": val(ref.get_text(" ", strip=True)) if ref else None,
                "raw_text": sq(row.get_text(" ", strip=True))})
    # A bad slug redirects to /fightcenter instead of 404ing, so an "event" with no
    # production block and no bouts is a miss, not an empty event.
    ev["parse_ok"] = bool(ev["production"]) or bool(ev["bouts"])
    seen = set()
    ev["referees"] = [r for r in ev["referees"]
                      if not (r["bout_slug"] in seen or seen.add(r["bout_slug"]))]
    return ev

# ------------------------------------------------------------- bout page ----
def parse_bout_page(html, url=None):
    s = soup_of(html)
    b = {"bout_url": url, "bout_id": None, "bout_slug": None, "bout_name": None,
         "result_line": None, "method": None, "duration": None, "awards": [],
         "info": {}, "info_raw": {}, "disclosed_pay": {}, "comparison": {},
         "event": {}, "event_links": [], "bout_links": [], "poster_url": None,
         "fighters": []}
    if url:
        b["bout_id"], b["bout_slug"] = id_slug(url, "bouts")
    h2 = s.find("h2")
    b["bout_name"] = val(h2.get_text(" ", strip=True)) if h2 else None

    # Result block is positional: [W / "X defeats Y" / L], [method, duration], [awards].
    # Read it by position -- method vocabularies are sport-specific ("Tech Fall", "Fall",
    # "Points" in wrestling) and a keyword whitelist misses them silently.
    holder = s.find(id="boutResultHolder")
    if holder:
        b["result_line"] = " | ".join(sq(x) for x in holder.stripped_strings if sq(x))
        rows = holder.find_all("div", recursive=False)
        if rows:
            # the W/L pills are spans too -- take the direct-child summary span
            summary = next((sp for sp in rows[0].find_all("span", recursive=False)
                            if len(sq(sp.get_text())) > 3), None)
            b["result_summary"] = val(summary.get_text(" ", strip=True)) if summary else None
            m = re.match(r"^(.*?)\s+(defeats|draws with|drew with|vs\.?)\s+(.*)$",
                         b["result_summary"] or "")
            if m and m.group(2) == "defeats":
                b["winner_name"], b["loser_name"] = m.group(1), m.group(3)
        if len(rows) > 1:
            spans = [val(sp.get_text(" ", strip=True)) for sp in rows[1].find_all("span")]
            b["method"] = spans[0] if spans else None
            b["duration"] = spans[1] if len(spans) > 1 else None
        for extra in rows[2:]:
            for line in (sq(x) for x in extra.stripped_strings):
                if line and line not in b["awards"]:
                    b["awards"].append(line)
    # finish detail: "3:34 Round 2 of 5, 8:34 Total" vs a distance "5 Rounds, 25:00 Total"
    if b["duration"]:
        m = re.match(r"^(\d+:\d{2})\s+Round\s+(\d+)\s+of\s+(\d+)", b["duration"])
        if m:
            b["finish"] = True
            b["finish_time"], b["finish_round"], b["scheduled_rounds"] = \
                m.group(1), int(m.group(2)), int(m.group(3))
        elif re.match(r"^\d+\s+Rounds?,", b["duration"]):
            b["finish"] = False

    ul = None
    for el in s.select("h3,h4"):
        if sq(el.get_text()) in ("Bout Information", "Fight Details"):
            ul = el.find_next("ul")
            break
    for raw, v, li in label_value_list(ul):
        b["info_raw"][raw] = v
        b["info"][canon(raw)] = v
        m = re.match(r"^(.*?)\s+Total Disclosed Pay:$", raw)   # label carries a name
        if m:
            b["disclosed_pay"][m.group(1)] = v
        if raw == "Event:":
            a = li.find("a", href=re.compile(r"/fightcenter/events/"))
            if a:
                eid, eslug = id_slug(a["href"], "events")
                b["event"] = {"event_id": eid, "event_slug": eslug,
                              "event_name": val(a.get_text(strip=True))}
        elif raw == "Location:":
            a = li.find("a", href=re.compile(r"^/regions/"))
            if a:
                b["region_slug"] = a["href"].replace("/regions/", "")
        elif raw == "Event Links:":
            b["event_links"] = links_in(li)
        elif raw == "Bout Links:":
            b["bout_links"] = links_in(li)

    # the "Fighter Comparison" heading is mobile-only (md:hidden) and gets stripped,
    # so anchor on the table id instead -- one per bout page.
    tbl = s.find(id="boutComparisonTable")
    if tbl is None:
        for el in s.select("h3,h4"):
            if sq(el.get_text()) == "Fighter Comparison":
                tbl = el.find_next("table")
                break
    b["comparison"] = comparison_table(tbl)
    poster = s.find("img", src=re.compile(r"/poster_images/"))
    if poster is not None:
        b["poster_url"] = poster.get("src")
    seen = set()
    for a in s.select('a[href*="/fightcenter/fighters/"]'):
        fslug = fighter_slug(a["href"])
        if not fslug or fslug in seen:
            continue
        seen.add(fslug)
        b["fighters"].append({"fighter_slug": fslug,
                              "name": val(a.get_text(strip=True))})
        if len(b["fighters"]) == 2:
            break
    b["parse_ok"] = bool(b["info"]) or bool(b["comparison"])
    return b

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
RECORD_ROW = re.compile(r"fighter-record-bout(\s|$)")

def record_date(year, month_day):
    """'2004', 'Sep 11' -> ('2004-09-11', 'full'); a row with only a year -> ('2004', 'year')."""
    y = re.fullmatch(r"\d{4}", year or "")
    if not y:
        return None, None
    m = re.fullmatch(r"([A-Z][a-z]{2})\s+(\d{1,2})", month_day or "")
    if m and m.group(1) in MONTHS:
        return f"{y.group(0)}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}", "full"
    return y.group(0), "year"

def parse_fighter_page(html, url=None):
    """A fighter page's record: every bout row, in every sport, including the ones that
    have no event page -- or no bout page -- anywhere else on Tapology.

    Not soup_of(): the record's detail rows (Event:, Weigh-In Result:) are only in the
    md:hidden copy, so stripping mobile duplicates would drop them. Each row is read
    from its own block, so no value is seen twice."""
    s = BeautifulSoup(html, "lxml")
    for t in s(["script", "style", "noscript"]):
        t.decompose()
    f = {"fighter_url": url, "fighter_slug": fighter_slug(url), "fighter_name": None, "page_title": None,
         "bouts": []}
    # the page has no h1: <title> is 'Anderson Silva ("The Spider") | MMA Fighter Page | Tapology'
    title = s.find("title")
    f["page_title"] = val(title.get_text()) if title else None
    if f["page_title"]:
        f["fighter_name"] = val(re.sub(r'\s*\(".*"\)\s*$', "", f["page_title"].split(" | ")[0]))
    for row in s.find_all("div", class_=RECORD_ROW, attrs={"data-bout-id": True}):
        bid = row["data-bout-id"]
        b = {"bout_id": int(bid) if bid.isdigit() else None, "sport": row.get("data-sport"),
             "division": row.get("data-division"), "status": row.get("data-status"),
             "result": None, "method_kind": None, "opponent_slug": None, "opponent_name": None,
             "record_before": None, "opponent_record_before": None, "bout_slug": None,
             "method_line": None, "promotion_id": None, "promotion_slug": None, "promotion_name": None,
             "year": None, "month_day": None, "date": None, "date_precision": None,
             "event_id": None, "event_slug": None, "event_name": None,
             "opponent_rank": None, "opponent_rank_url": None,
             "details": {}, "details_raw": {}}
        res = row.find("div", class_="result")
        if res:
            cells = [c for c in res.find_all("div", recursive=False)]
            b["result"] = val(cells[0].get_text(strip=True)) if cells else None
            b["method_kind"] = val(cells[1].get_text(strip=True)) if len(cells) > 1 else None
        opp = row.find("a", title=re.compile(r" Fighter Page$"))
        if opp:
            b["opponent_slug"], b["opponent_name"] = fighter_slug(opp["href"]), val(opp.get_text(strip=True))
        else:   # an opponent with no profile: the name is plain text in the same cell
            cell = row.find("div", class_=re.compile(r"font-bold text-neutral-800"))
            b["opponent_name"] = val(cell.get_text(" ", strip=True)) if cell else None
        for sp in row.find_all("span", title=True):
            if sp["title"] == "Fighter Record Before Fight":
                b["record_before"] = val(sp.get_text(strip=True))
            elif sp["title"] == "Opponent Record Before Fight":
                b["opponent_record_before"] = val(sp.get_text(strip=True))
        bout = row.find("a", title="Bout Page")
        if bout:
            b["bout_slug"] = id_slug(bout["href"], "bouts")[1]
            b["method_line"] = val(bout.get_text(" ", strip=True))
        else:   # no bout page: the desktop method cell is bare text ("Win")
            cell = row.find("div", class_=re.compile(r"text-neutral-700 text-xs font-bold"))
            b["method_line"] = val(cell.get_text(" ", strip=True)) if cell else None
        promo = row.find("a", title="Promotion Page")
        if promo:
            b["promotion_id"], b["promotion_slug"] = id_slug(promo["href"], "promotions")
            b["promotion_name"] = val(promo.get_text(strip=True))
        when = row.find("span", class_=re.compile(r"text-tap_3 font-bold"))
        if when:
            b["year"] = val(when.get_text(strip=True))
            nxt = when.find_next_sibling("span")
            b["month_day"] = val(nxt.get_text(strip=True)) if nxt else None
        b["date"], b["date_precision"] = record_date(b["year"], b["month_day"])
        # the opponent's ranking at the time, a badge linking to that date's historical table
        rank = row.find("a", href=re.compile(r"^/rankings/"))
        if rank:
            b["opponent_rank_url"] = rank["href"]
            n = re.search(r"#\s*(\d+)", rank.get_text("", strip=True))
            b["opponent_rank"] = int(n.group(1)) if n else None
        details = s.find(id=f"detail-rows-{bid}")
        for line in details.find_all("div", recursive=False) if details else []:
            lab = line.find("span", class_="font-bold")
            if not lab:
                continue
            raw = sq(lab.get_text())
            if not raw.endswith(":") or raw in b["details_raw"]:
                continue
            rest = line.__copy__()
            rest.find("span", class_="font-bold").decompose()
            v = val(rest.get_text(" ", strip=True))
            b["details_raw"][raw], b["details"][canon(raw)] = v, v
            if raw == "Event:":
                a = line.find("a", href=re.compile(r"/fightcenter/events/"))
                if a:
                    b["event_id"], b["event_slug"] = id_slug(a["href"], "events")
                    b["event_name"] = val(a.get_text(strip=True))
        f["bouts"].append(b)
    f["parse_ok"] = bool(f["bouts"])
    return f

if __name__ == "__main__":
    path = Path(sys.argv[1])
    url = sys.argv[2] if len(sys.argv) > 2 else None
    html = path.read_text(encoding="utf-8", errors="replace")
    if "/fighters/" in (url or ""):
        print(json.dumps(parse_fighter_page(html, url), ensure_ascii=False, indent=2))
        sys.exit()
    fn = parse_bout_page if "/bouts/" in (url or "") or "bout-" in path.name else parse_event
    print(json.dumps(fn(html, url), ensure_ascii=False, indent=2))
