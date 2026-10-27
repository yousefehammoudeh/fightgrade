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

LINK = re.compile(r"^(event|decision)/(\d+)/([^/?#]*)")


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
