"""English Wikipedia's UFC event articles: every bout change their Background sections list -> the lake -> LANDING.

    .venv/bin/python -m pipeline.wikipedia_events                      # DEV: events within 60 days of today
    .venv/bin/python -m pipeline.wikipedia_events --backfill           # DEV: every event not archived yet
    .venv/bin/python -m pipeline.wikipedia_events --title "UFC 294" --dry-run
    .venv/bin/python -m pipeline.wikipedia_events --replay             # re-parse the archive, load LANDING

"List of UFC events" names every event article. Each article's wikitext comes
from the MediaWiki API (action=parse&prop=wikitext|revid) and is archived as
served, as JSON, before anything is parsed from it. Its Background section is
formulaic, one paragraph per bout:

    A middleweight bout between [[Ikram Aliskerov]] and [[Nassourdine Imavov]] was
    expected to take place at the event.<ref>..</ref> However, Imavov withdrew due to
    visa issues and was replaced by ... [[Warlley Alves]].<ref>{{cite web |date=October 7, 2023 ..}}</ref>

One row per sentence that describes a change (a withdrawal, a replacement, a
cancellation, a bout moved, a weight miss), to LANDING lnd_wikipedia_bout_changes:
the pairing as it stood before the change, the withdrawn fighter, the
replacement, the reason as Wikipedia states it, and each cited source's url,
title and date. The citation's date is when the change was first reported. The
fight data decides whether a change happened; this source dates and explains it.

The parser reads sentences with rules, not a model: a field it cannot find is
NULL, and `sentence` keeps the text for whoever needs more.

Wikimedia's API etiquette: serial requests, maxlag=5 (wait when the servers say
they lag), gzip, MIN_INTERVAL between request starts, and a user agent with a
contact (the user's choice, 2026-10-31). Content is CC BY-SA 4.0: every row
keeps its revision id, so it can be attributed to the exact revision.

The weekly run reads the list and every event dated within WINDOW_DAYS of today
(~20 requests): a card's changes are written in the weeks around it. The
backfill reads every event not archived yet; one cut short is simply run again.
"""
import argparse, gzip, html, json, re, shutil, sys, time, urllib.error, urllib.parse, urllib.request
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from .lake import Lake, environment, new_run_id, zone
from .tapology import Budget, Stop, parsed_key, roundtrip_mismatches, snowflake_cursor, stage

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "_crawlruns" / "wikipedia"

API = "https://en.wikipedia.org/w/api.php"
LIST_PAGE = "List of UFC events"
LIST_SECTIONS = ("Scheduled events", "Past events")
LICENSE = "CC BY-SA 4.0"

# Wikimedia's policy asks for a contact; the user chose this one (2026-10-31).
USER_AGENT = "FightGrade-wikipedia/1.0 (private, non-commercial MMA research; yousefhammoudeh567@gmail.com)"

TABLE = "wikipedia_bout_changes"
DBT_SELECT = "path:models/landing/wikipedia"

MIN_INTERVAL = 2.0          # seconds between request starts
TIMEOUT = 60
MAXLAG = 5                  # seconds of replication lag past which the API asks us to wait
MAXLAG_TRIES = 6
BACKOFF = (30, 90)          # a 5xx or a dropped connection, before trying the page again
RATE_LIMITED = (429,)
BLOCKED = (401, 403)
WINDOW_DAYS = 60
WEEKLY_BUDGET = 60          # the list and ~20 events
BACKFILL_BUDGET = 1200      # ~810 events and the list
MAX_FETCH_MINUTES = 90
LOCK_STALE_HOURS = 3


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def page_name(title: str) -> str:
    return urllib.parse.quote(title.replace(" ", "_"), safe="")


def page_title(name: str) -> str:
    return urllib.parse.unquote(name).replace("_", " ")


def raw_json_key(kind: str, fetch_date: str, title: str, env: str | None = None) -> str:
    return f"{zone(env)}raw-json/wikipedia/{kind}/fetch_date={fetch_date}/{page_name(title)}.json.gz"


class Fetcher:
    """The polite API client: one request at a time, MIN_INTERVAL start to start, maxlag honoured."""

    def __init__(self, budget: Budget, interval: float):
        self.budget, self.interval, self.last = budget, interval, 0.0
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Accept-Encoding": "gzip"}

    def get(self, params: dict) -> tuple[int | str, bytes | None, dict]:
        """(status, body, headers). A transport error or timeout is 'FAIL'."""
        self.budget.take()
        wait = self.interval - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        url = API + "?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=self.headers), timeout=TIMEOUT) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return r.status, body, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, None, dict(e.headers or {})
        except (urllib.error.URLError, TimeoutError, OSError):
            return "FAIL", None, {}

    def parse(self, title: str) -> tuple[str, bytes | None]:
        """('ok', json) | ('missing', json) | ('failed: ...', None). A limit or a refusal stops the run."""
        params = {"action": "parse", "page": title, "prop": "wikitext|revid", "redirects": "1",
                  "format": "json", "formatversion": "2", "maxlag": str(MAXLAG)}
        lagged = failed = 0
        while True:
            status, body, headers = self.get(params)
            if status in RATE_LIMITED:
                raise Stop(f"wikipedia: rate limited (HTTP {status})")
            if status in BLOCKED:
                raise Stop(f"wikipedia: refused (HTTP {status}); is the user agent blocked?")
            if status == 200 and body is not None:
                try:
                    err = (json.loads(body).get("error") or {}).get("code")
                except ValueError:
                    return "failed: not JSON", None
                if err == "maxlag" and lagged < MAXLAG_TRIES:
                    lagged += 1
                    pause = int(headers.get("Retry-After") or MAXLAG)
                    log(f"  {title}: servers lagging -> wait {pause}s")
                    time.sleep(pause)
                    continue
                if err == "missingtitle":
                    return "missing", body
                if err:
                    return f"failed: {err}", None
                return "ok", body
            if failed == len(BACKOFF):
                return f"failed: {status}", None
            log(f"  {title}: {status} -> wait {BACKOFF[failed]}s")
            time.sleep(BACKOFF[failed])
            failed += 1


# ---------------------------------------------------------------- wikitext

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
HEADING = re.compile(r"^(=+)\s*(.*?)\s*\1\s*$", re.M)
COMMENT = re.compile(r"<!--.*?-->", re.S)
REF_ATTR_NAME = re.compile(r"""\bname\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s/>]+))""")
REF = re.compile(r"<ref(\s[^>]*?)?(?:/>|>(.*?)</ref\s*>)", re.S | re.I)
LINK = re.compile(r"\[\[([^\[\]|]+)(?:\|([^\[\]]*))?\]\]")
EXT_LINK = re.compile(r"\[(?:https?:)?//[^\s\]]+\s+([^\]]*)\]")
TAG = re.compile(r"</?[a-zA-Z][^>]*>")
PLACEHOLDER = re.compile(r"\x00(\d+)\x00")


def section(wikitext: str, name: str) -> str | None:
    """The text under the heading `name` (any level), up to the next heading of its level or above."""
    heads = list(HEADING.finditer(wikitext))
    for i, h in enumerate(heads):
        if h.group(2).strip().lower() == name.lower():
            level = len(h.group(1))
            end = next((n.start() for n in heads[i + 1:] if len(n.group(1)) <= level), len(wikitext))
            return wikitext[h.end():end]
    return None


def split_top(s: str, sep: str = "|") -> list[str]:
    """Split on sep outside [[..]] and {{..}}."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(s):
        two = s[i:i + 2]
        if two in ("{{", "[["):
            depth += 1; cur.append(two); i += 2; continue
        if two in ("}}", "]]") and depth:
            depth -= 1; cur.append(two); i += 2; continue
        if s[i] == sep and depth == 0:
            out.append("".join(cur)); cur = []; i += 1; continue
        cur.append(s[i]); i += 1
    out.append("".join(cur))
    return out


def templates(s: str) -> list[tuple[int, int, str]]:
    """Top-level {{...}} spans: (start, end, inner)."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(s) - 1:
        if s[i:i + 2] == "{{":
            if depth == 0:
                start = i
            depth += 1; i += 2; continue
        if s[i:i + 2] == "}}" and depth:
            depth -= 1; i += 2
            if depth == 0:
                out.append((start, i, s[start + 2:i - 2]))
            continue
        i += 1
    return out


def render_template(inner: str) -> str:
    """The few inline templates whose text belongs in a sentence; every other one is dropped."""
    parts = [p.strip() for p in split_top(inner)]
    name, args = parts[0].lower().strip(), [p for p in parts[1:] if "=" not in p.split("[[")[0]]
    if name in ("sortname", "sort name") and len(args) >= 2:
        return f"{args[0]} {args[1]}"
    if name in ("nowrap", "nobr", "abbr", "small", "lang", "nts", "nobold", "noitalic") and args:
        return args[-1] if name == "lang" else args[0]
    if name in ("convert", "cvt") and len(args) >= 2:
        return f"{args[0]} {args[1]}"
    if name in ("ndash", "snd"):
        return " – "
    return ""


def strip_templates(s: str) -> str:
    for start, end, inner in reversed(templates(s)):
        s = s[:start] + render_template(inner) + s[end:]
    return s


def strip_files(s: str) -> str:
    """[[File:..]] and [[Image:..]], captions and their nested links included."""
    out, i = [], 0
    while True:
        m = re.search(r"\[\[\s*(?:File|Image)\s*:", s[i:], re.I)
        if not m:
            return "".join(out) + s[i:]
        start = i + m.start()
        out.append(s[i:start])
        depth, j = 0, start
        while j < len(s):
            if s[j:j + 2] == "[[":
                depth += 1; j += 2; continue
            if s[j:j + 2] == "]]":
                depth -= 1; j += 2
                if depth == 0:
                    break
                continue
            j += 1
        i = j


def cite_date(raw: str | None) -> str | None:
    """'October 10, 2023', '10 October 2023', '2023-10-10' -> ISO. NULL when the day is not given."""
    if not raw:
        return None
    s = raw.strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = (re.fullmatch(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", s)
             or re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)\.?,?\s+(\d{4})", s))
        if not m:
            return None
        a, b, y = m.groups()
        mon, d = (a, b) if a[0].isalpha() else (b, a)
        mo, d, y = MONTHS.get(mon[:3].lower()), int(d), int(y)
        if mo is None:
            return None
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def citations(ref_body: str) -> list[dict]:
    """Every cite template (and bare [url title]) in one <ref>."""
    out = []
    for _, _, inner in templates(ref_body):
        parts = split_top(inner)
        if not parts[0].strip().lower().startswith("cite"):
            continue
        p = {}
        for part in parts[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                p[k.strip().lower()] = COMMENT.sub("", v).strip() or None
        out.append({"url": p.get("url"), "title": plain(p.get("title") or "") or None, "date": p.get("date"),
                    "date_iso": cite_date(p.get("date")),
                    "website": plain(p.get("website") or p.get("work") or p.get("publisher") or p.get("newspaper")
                                     or p.get("magazine") or "") or None,
                    "access_date": p.get("access-date") or p.get("accessdate")})
    if not out:
        m = re.search(r"\[(https?://[^\s\]]+)\s*([^\]]*)\]", ref_body)
        if m:
            out.append({"url": m.group(1), "title": m.group(2).strip() or None, "date": None, "date_iso": None,
                        "website": None, "access_date": None})
    return out


def ref_name(attrs: str | None) -> str | None:
    m = REF_ATTR_NAME.search(attrs or "")
    return next((g for g in m.groups() if g), None) if m else None


def named_refs(wikitext: str) -> dict[str, str]:
    """name -> the body of the <ref name=..>body</ref> that defines it, anywhere on the page."""
    out = {}
    for m in REF.finditer(COMMENT.sub("", wikitext)):
        name = ref_name(m.group(1))
        if name and m.group(2) and name not in out:
            out[name] = m.group(2)
    return out


def plain(s: str, links: dict | None = None) -> str:
    """Wikitext -> text. Each [[target|label]] becomes its label; links (label -> target) collects them."""
    s = strip_files(strip_templates(s))

    def link(m):
        target, label = m.group(1).strip(), (m.group(2) or m.group(1)).strip()
        if links is not None:
            links[label] = target.split("#")[0]
        return label
    s = LINK.sub(link, s)
    s = EXT_LINK.sub(lambda m: m.group(1), s)
    s = TAG.sub("", s).replace("'''", "").replace("''", "")
    return re.sub(r"\s+", " ", html.unescape(s).replace("\xa0", " ")).strip()


# ---------------------------------------------------------------- sentences

ABBREV = re.compile(r"\b(vs|Jr|Sr|St|Dr|Mr|Mrs|Ms|No|Mt|Ft|Bros|Inc|Lt|Sgt|approx|ca)\.|\b([A-Z])\.(?=\s*[A-Z]?\.?\s*[A-Z])")
SENT_END = re.compile(r"[.!?][\"'”’)]*((?:\s*\x00\d+\x00)*)(?=\s+[^a-z\s]|\s*$)|:((?:\s*\x00\d+\x00)*)(?=\n)")


def sentences(text: str) -> list[str]:
    """Split a paragraph whose refs are \x00i\x00 placeholders; a sentence keeps the refs after its stop."""
    protected = ABBREV.sub(lambda m: m.group(0).replace(".", "\x01"), text)
    out, start = [], 0
    for m in SENT_END.finditer(protected):
        out.append(protected[start:m.end()].strip())
        start = m.end()
    if protected[start:].strip():
        out.append(protected[start:].strip())
    return [s.replace("\x01", ".") for s in out if s]


WEIGHTS = (r"(?:women's )?(?:light |super |junior )?(?:heavyweight|welterweight|middleweight|lightweight|featherweight|"
           r"bantamweight|flyweight|strawweight|atomweight|catchweight|openweight|cruiserweight)")
BOUT_WORDS = r"(?:bout|rematch|fight|matchup|match|trilogy|title bout|title fight|contest|clash|showdown|pairing)"
PARTICLES = {"de", "da", "das", "do", "dos", "du", "van", "von", "der", "den", "la", "le", "bin", "al", "el", "di",
             "del", "della", "e", "y", "ten", "ter", "st."}
# Capitalized words that open a fighter's description, never the name itself.
STOP = {
    "ufc", "champion", "championship", "champions", "titleholder", "ultimate", "fighter", "tuf", "bellator", "pfl",
    "strikeforce", "wec", "pride", "one", "ksw", "invicta", "lfa", "cage", "warriors", "rizin", "m-1", "acb", "aca",
    "road", "season", "series", "contender", "tournament", "grand", "prix", "interim", "former", "olympic", "olympian",
    "medalist", "gold", "silver", "bronze", "world", "bjj", "jiu-jitsu", "judo", "wrestling", "boxing", "kickboxing",
    "glory", "however", "the", "a", "an", "at", "in", "on", "but", "and", "after", "then", "subsequently", "also",
    "meanwhile", "eventually", "instead", "later", "in", "his", "her", "their", "former", "current", "undefeated",
    "brazilian", "american", "russian", "promotional", "newcomer", "veteran", "event", "weigh-ins", "fight", "night",
    "heavyweight", "welterweight", "middleweight", "lightweight", "featherweight", "bantamweight", "flyweight",
    "strawweight", "atomweight", "light", "women's", "men's", "featherweight,", "co-main", "main", "dana", "white's",
    "abu", "dhabi", "las", "vegas", "he", "she", "they", "it", "both", "this", "that", "while", "dwcs",
}
PRONOUNS = {"he", "she", "they", "him", "her", "his", "hers", "it"}


def is_name_word(w: str) -> bool:
    core = w.strip(",;:()\"'“”")
    return bool(core) and (core[0].isupper() or core.lower() in PARTICLES) and core.lower() not in STOP


def clean_name(words: list[str]) -> str | None:
    while words and words[0] in PARTICLES:              # 'da Silva' -> 'Silva', but 'Van' is a name
        words = words[1:]
    name = " ".join(words).strip(",;:()\"'“”")
    if name.endswith(".") and not re.search(r"(?:\b[A-Z]|Jr|Sr|St)\.$", name):
        name = name[:-1]
    name = re.sub(r"(?:'s|’s)$", "", name).strip()
    return name or None


def last_name_phrase(s: str) -> str | None:
    """The person named at the end of s: 'However, former champion [[Charles Oliveira]]' -> 'Charles Oliveira'."""
    words, out = s.split(), []
    for i, w in enumerate(reversed(words)):
        if i and w.endswith((",", ";", ":")):
            break
        if not is_name_word(w) or len(out) == 5:
            break
        out.insert(0, w)
        if w.startswith(("(", "\"", "“")):
            break
    return clean_name(out)


def first_name_phrase(s: str) -> str | None:
    """The first person named in s: 'promotional newcomer Brendon Marotte, who' -> 'Brendon Marotte'."""
    words, out = s.split(), []
    for w in words:
        if not out and not is_name_word(w):
            continue
        if out and not is_name_word(w):
            break
        out.append(w)
        if w.endswith((",", ";", ":", ".")) and not re.fullmatch(r"(?:[A-Z]\.)+|Jr\.|Sr\.|St\.", w.rstrip(",;:")):
            break
        if len(out) == 5:
            break
    return clean_name(out)


def fold(s: str | None) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def same_fighter(written: str | None, full: str | None) -> bool:
    """'Imavov' is 'Nassourdine Imavov'; 'Rodriguez' is 'Christian Rodriguez'."""
    a, b = fold(written), fold(full)
    if not a or not b:
        return False
    return a == b or b.endswith(" " + a) or a.endswith(" " + b) or a.split()[-1] == b.split()[-1]


PAIR_BETWEEN = re.compile(rf"\b{BOUT_WORDS}\b[^.]*?\bbetween\s+(?P<rest>.+)", re.I)
PAIR_VERB = re.compile(r"\s+(?:was|were|is|are|had|has|headlined|co-headlined|served|took|initially|originally|also|"
                       r"would|will|became|marked|did|ended|for|to|in|at|on)\b|[,;(]|\.$|$")
PAIR_FACE = re.compile(r"^(?P<a>.+?)\s+(?:was|were|had been)\s+(?:originally |initially |previously |briefly )?"
                       r"(?:expected|scheduled|set|booked|slated|linked|rumored|due)\s+to\s+"
                       r"(?:face|meet|fight|take on|rematch|defend (?:\w+ ){0,4}against)\s+(?P<b>.+)$", re.I)

PAIR_AND = re.compile(r"^(?P<ab>.+?\s+and\s+.+?)\s+(?:were|are|had been)\s+(?:\w+\s+){0,2}(?:expected|scheduled|set|booked|"
                      r"slated|linked|due|targeted)\s+to\s+(?:meet|fight|face|compete|square off|clash|collide|"
                      r"headline|rematch|take place)\b", re.I)


def pairing(s: str) -> tuple[str, str] | None:
    """('A', 'B') from 'A ... bout between A and B was expected ...' or 'A was expected to face B'."""
    m = PAIR_BETWEEN.search(s)
    if m:
        rest = m.group("rest")
        stop = PAIR_VERB.search(rest, rest.find(" and ") + 5 if " and " in rest else 0)
        span = rest[:stop.start()] if stop else rest
        cuts = [c.start() for c in re.finditer(r" and ", span)]
        for c in reversed(cuts):
            a, b = last_name_phrase(span[:c]), last_name_phrase(span[c + 5:])
            if a and b:
                return a, b
        return None
    m = PAIR_FACE.match(s)
    if m:
        a, b = last_name_phrase(m.group("a")), first_name_phrase(m.group("b"))
        if a and b:
            return a, b
    m = PAIR_AND.match(s)                           # 'A and B were expected to meet in a flyweight bout'
    if m:
        ab = m.group("ab")
        for c in reversed([c.start() for c in re.finditer(r" and ", ab)]):
            a, b = last_name_phrase(ab[:c]), last_name_phrase(ab[c + 5:])
            if a and b:
                return a, b
    return None


WITHDREW = re.compile(
    r"\b(?:withdrew|withrew|withdrawn|withdrawal due|had to withdraw|was forced to withdraw|was forced (?:out|off)|were forced out|"
    r"pulled out|was pulled|were pulled|dropped out|bowed out|was removed|were removed|was scratched|"
    r"was sidelined|was not (?:medically )?cleared|was deemed (?:unfit|medically unfit)|was unable to (?:compete|"
    r"fight|make|obtain|travel|secure)|could not (?:compete|make|obtain|travel)|stepped away|fell ill|"
    r"was hospitali[sz]ed|was (?:taken|rushed) to (?:the )?hospital|was denied|was ruled out|was out)\b", re.I)
REPLACED = re.compile(r"\b(?:was|were|been) replaced\b|\breplaced by\b", re.I)
STEPPED_IN = re.compile(r"^(?P<who>.+?)\s+(?:stepped in|steps in|stepped up|was named|was brought in|agreed)\s+"
                        r"(?:on short notice\s+)?(?:as (?:a|the|his|her) (?:late |short-notice )?replacement|"
                        r"to (?:replace|face))\s+(?P<rest>.*)$", re.I)
CANCELLED = re.compile(r"\b(?:cancell?ed|scrapped|called off|fell through|nixed|scratched from the card|"
                       r"removed from the card|did not take place|was abandoned)\b", re.I)
MOVED = re.compile(r"\b(?:was|were) (?:moved|rescheduled|postponed|pushed back|shifted|delayed|rebooked|relocated)\b",
                   re.I)
WEIGHED = re.compile(r"\bweighed in at (?P<lbs>\d+(?:\.\d+)?)\s*(?:pounds|lb)", re.I)
MISSED = re.compile(r"\b(?:missed weight|missed the (?:\w+ ){0,3}(?:limit|mark)|failed to make weight|"
                    r"came in (?:at )?(?P<lbs>\d+(?:\.\d+)?)?\s*(?:pounds )?over)\b", re.I)
OVER = re.compile(r"\bover the\b|\babove the\b|\bover (?:the )?(?:\w+ )?(?:limit|mark)\b", re.I)
REASON = re.compile(
    r"\b(?:due to|because of|citing|as a result of|owing to|following|after (?:he |she |it |they )?"
    r"(?:suffered|sustained|underwent|tested|was|were|tore|broke|injured|fell|had|failed|did|required|"
    r"needed|contracted|withdrew|picked up|hurt|dislocated|fractured|experienced|developed|got)|"
    r"for (?:undisclosed|unknown|unspecified|personal|medical|visa|health|family|undisclosed)\b|"
    r"with an? (?:\w+ ){0,3}(?:injury|illness|infection))", re.I)
REASON_END = re.compile(r"\s+and (?:was|were|is|he|she|they|the|subsequently|later)\b|, (?:and |as |but |which |"
                        r"with |so |the (?:bout|fight|pairing|matchup|rematch|contest)\b)|;|\.$|$| \(| and replaced",
                        re.I)
REASON_CATEGORIES = [
    ("visa", r"visa|immigration|travel|passport|entry|border"),
    ("suspension", r"usada|drug|doping|test(?:ed)? positive|positive test|suspen|banned|anti-doping|commission|license"),
    ("weight_cut", r"weight cut|cutting weight|weight issues|make weight|weight-cut"),
    ("injury", r"injur|surgery|torn|tear|broke|broken|fractur|knee|hand|elbow|shoulder|back|neck|rib|acl|mcl|"
               r"concussion|hamstring|ankle|foot|eye|orbital|nose|hernia|dislocat|sprain|cut"),
    ("illness", r"ill|sick|hospital|infection|covid|virus|flu|staph|disease|pneumonia|kidney"),
    ("medical", r"medical|not cleared|medically|health"),
    ("personal", r"personal|family|birth|death|bereavement|passed away"),
    ("legal", r"legal|arrest|charge|jail|court|police"),
    ("undisclosed", r"undisclosed|unknown|unspecified|undeclared"),
]
PREVIOUSLY = re.compile(r"\b(?:previously|originally|initially|first) (?:scheduled|expected|booked|set|linked|"
                        r"slated)\b|\bhad been (?:scheduled|expected|booked)\b|\bwere previously\b", re.I)
EVENT_LINK = re.compile(r"^(?:UFC|The Ultimate Fighter|Ultimate Fighting|UFC on|UFC Fight Night)\b")


def reason_of(s: str) -> str | None:
    m = REASON.search(s)
    if not m:
        return None
    rest = s[m.start():]
    end = REASON_END.search(rest, len(m.group(0)))
    return rest[:end.start() if end else len(rest)].strip(" ,.") or None


def reason_category(reason: str | None) -> str | None:
    if not reason:
        return None
    low = reason.lower()
    for name, pat in REASON_CATEGORIES:
        if re.search(rf"\b(?:{pat})", low):
            return name
    return "other"


ADVERBS = re.compile(r"(?:\s+(?:once|again|also|then|later|subsequently|ultimately|eventually|reportedly|"
                     r"officially|unfortunately|himself|herself|too|initially|had|has|was|would|ended up|"
                     r"in turn|in the end|as well))+\s*$", re.I)


def withdrawn_of(s: str) -> str | None:
    """Who left the bout: the subject of 'withdrew' / 'was replaced', or 'due to X's ...'."""
    m = re.search(r"\bdue to ([A-Z][\w\-.' ]*?)(?:'s|’s)\b", s)
    if m and m.group(1).split()[-1].lower() not in PRONOUNS:
        return m.group(1).strip()
    for pat in (WITHDREW, re.compile(r"\b(?:was|were) replaced\b", re.I)):
        for m in pat.finditer(s):
            who = last_name_phrase(ADVERBS.sub("", re.sub(r"[\[\]]", "", s[:m.start()])))
            if who and who.lower() not in PRONOUNS:
                return who
    return None


def replacement_of(s: str) -> tuple[str | None, str | None]:
    """(replacement, who they replaced if this sentence says so)."""
    m = re.search(r"\breplaced by\s+(?P<rest>.+)$", s, re.I)
    if m:
        rest = re.sub(r"\s*\([^)]*\)", "", m.group("rest"))
        seg = re.split(r",| who | in an? | at | for | on | after | as | instead|;| and (?:was|were)\b|"
                       r" and the (?:bout|fight|pairing|matchup|rematch)\b", rest)[0]
        return last_name_phrase(seg.rstrip(". ")), None
    m = STEPPED_IN.match(s)
    if m:
        return last_name_phrase(m.group("who")), first_name_phrase(m.group("rest"))
    return None, None


def change_row(s: str) -> dict | None:
    """One sentence -> the change it describes, or None if it describes none."""
    weighed, missed = WEIGHED.search(s), MISSED.search(s)
    weight_miss = bool(missed or (weighed and OVER.search(s)))
    replaced = bool(REPLACED.search(s) or STEPPED_IN.match(s))
    withdrew = bool(WITHDREW.search(s))
    cancelled = bool(CANCELLED.search(s))
    moved = bool(MOVED.search(s))
    if not (weight_miss or replaced or withdrew or cancelled or moved):
        return None
    if weight_miss:
        at = (weighed or missed).start()
        fighter = last_name_phrase(re.sub(r"^At the (?:\w+ )?weigh-ins?,\s*", "", s[:at], flags=re.I))
        lbs = (weighed and weighed.group("lbs")) or (missed and missed.group("lbs"))
        if not fighter:                                 # 'two fighters missed weight:' heads a list
            return None
        return {"change_type": "weight_miss", "withdrawn_as_written": fighter, "replacement_as_written": None,
                "weighed_lbs": float(lbs) if lbs else None, "withdrew": False, "replaced": False,
                "cancelled": cancelled, "moved": False, "reason": None}
    replacement, replaced_who = replacement_of(s) if replaced else (None, None)
    withdrawn = replaced_who or withdrawn_of(s)
    kind = ("replacement" if replaced else "withdrawal" if withdrew else "cancellation" if cancelled else "moved")
    return {"change_type": kind, "withdrawn_as_written": withdrawn, "replacement_as_written": replacement,
            "weighed_lbs": None, "withdrew": withdrew or (replaced and withdrawn is not None), "replaced": replaced,
            "cancelled": cancelled, "moved": moved, "reason": reason_of(s)}


def paragraphs(text: str) -> list[str]:
    """One per line; a bulleted line ('*Souza weighed in at 117 pounds ...') joins the line
    above it, on a line of its own, so the list shares its lead-in's pairing and refs."""
    out: list[str] = []
    for line in strip_files(strip_templates(text)).split("\n"):
        bullet = re.match(r"^[*#:;]+\s*", line)
        line = line[bullet.end():] if bullet else line
        if not line.strip():
            continue
        if bullet and out:
            out[-1] += "\n" + line.strip()
        else:
            out.append(line.strip())
    return out


def background_changes(wikitext: str, title: str) -> tuple[list[dict], dict]:
    """Every change sentence in the Background section, with its pairing and citations."""
    bg = section(wikitext, "Background")
    if bg is None:
        return [], {"no_background": 1}
    refs_by_name = named_refs(wikitext)
    cites: list[list[dict]] = []

    def ref(m):
        name, body = ref_name(m.group(1)), m.group(2)
        body = body if body else refs_by_name.get(name or "", "")
        cites.append([{**c, "ref_name": name} for c in citations(body)])
        return f"\x00{len(cites) - 1}\x00"
    text = REF.sub(ref, COMMENT.sub("", bg))
    rows, counts = [], Counter(sentences_seen=0)
    for p_i, para in enumerate(paragraphs(text)):
        pair, pair_sentence, pair_cites, pair_links, last_out = None, None, [], {}, None
        sents = sentences(para)
        own = [[int(i) for i in PLACEHOLDER.findall(x)] for x in sents]
        for s_i, raw in enumerate(sents):
            counts["sentences_seen"] += 1
            links: dict = {}
            # A sentence with no ref of its own is covered by the next ref in its paragraph, or,
            # at its end (a bulleted list after 'three fighters missed weight:'), by the last one before.
            after = next((r for r in own[s_i + 1:] if r), [])
            before = next((r for r in reversed(own[:s_i]) if r), [])
            refs = own[s_i] or after or before
            cited_in = ("sentence" if own[s_i] else "following" if after else "preceding" if before else None)
            s = PLACEHOLDER.sub("", plain(raw, links)).strip()
            s = re.sub(r"\s+([.,;])", r"\1", s)
            found = pairing(s)
            if found:
                pair, pair_sentence, pair_cites, pair_links = found, s, [c for i in refs for c in cites[i]], links
            ch = change_row(s)
            if not ch:
                continue
            all_links = {**pair_links, **links}
            a, b = pair or (None, None)
            withdrawn, replacement = ch.pop("withdrawn_as_written"), ch.pop("replacement_as_written")
            if withdrawn is None and ch["change_type"] in ("replacement", "withdrawal") and last_out \
                    and re.match(r"^(?:\w+,\s*)?(?:he|she|they|the (?:latter|former))\b", s, re.I):
                withdrawn = last_out
            full = next((f for f in (a, b) if same_fighter(withdrawn, f)), None)
            other = (b if full == a else a) if full else None
            refers = None
            if PREVIOUSLY.search(s):
                refers = next((t for t in links.values() if EVENT_LINK.match(t) and fold(t) != fold(title)), None)
            rows.append({
                "paragraph": p_i, "sentence_index": s_i, "sentence": s, **ch,
                "reason_category": reason_category(ch["reason"]),
                "weight_class": (lambda m: m.group(0).lower() if m else None)(
                    re.search(rf"\b{WEIGHTS}\b", pair_sentence or s, re.I)),
                "fighter_a": a, "fighter_a_wiki": all_links.get(a), "fighter_b": b,
                "fighter_b_wiki": all_links.get(b),
                "withdrawn_fighter": full or withdrawn, "withdrawn_fighter_wiki": all_links.get(full or withdrawn),
                "withdrawn_as_written": withdrawn, "opponent": other,
                "replacement_fighter": replacement, "replacement_fighter_wiki": all_links.get(replacement),
                "refers_to_event": refers,
                "citations": [c for i in refs for c in cites[i]], "cited_in": cited_in,
                "pairing_sentence": pair_sentence if pair_sentence != s else None,
                "pairing_citations": pair_cites if pair_sentence != s else [],
            })
            counts[f"changes_{ch['change_type']}"] += 1
            if ch["change_type"] != "weight_miss" and (full or withdrawn):
                last_out = full or withdrawn
            if full and replacement and ch["change_type"] == "replacement" and refers is None:
                pair = (replacement, other) if full == a else (other, replacement)
                pair_links = {**all_links}
    return rows, dict(counts)


# ---------------------------------------------------------------- the list

DTS = re.compile(r"\{\{\s*dts\s*\|\s*(\d{4})\s*\|\s*([A-Za-z]+|\d{1,2})\s*\|\s*(\d{1,2})", re.I)


def listed_events(wikitext: str) -> list[dict]:
    """[{title, label, event_date, listed_in}] for every row of the list's event tables that links an article."""
    out, seen = [], set()
    for name in LIST_SECTIONS:
        body = section(wikitext, name) or ""
        for row in re.split(r"\n\|-", COMMENT.sub("", REF.sub("", body)))[1:]:
            link, dts = LINK.search(row), DTS.search(row)
            if not link or not dts:
                continue
            y, mo, d = dts.groups()
            mo = int(mo) if mo.isdigit() else MONTHS.get(mo[:3].lower())
            try:
                when = date(int(y), mo, int(d)).isoformat()
            except (TypeError, ValueError):
                when = None
            title = link.group(1).strip().split("#")[0]
            if title in seen:
                continue
            seen.add(title)
            out.append({"title": title, "label": (link.group(2) or title).strip(), "event_date": when,
                        "listed_in": name})
    return out


# ---------------------------------------------------------------- crawl

def archived_titles(lake: Lake, zones: tuple[str, ...]) -> set[str]:
    """Event titles with an archived page (or a recorded missing one) in any of the zones."""
    out = set()
    for z in zones:
        for blob in lake.bucket.list_blobs(prefix=f"{z}raw-json/wikipedia/event/"):
            name = blob.name.rsplit("/", 1)[-1]
            for ext in (".json.gz", ".missing.json"):
                if name.endswith(ext):
                    out.add(page_title(name[: -len(ext)]))
    return out


@contextmanager
def wiki_lock(lake: Lake | None):
    """One run at a time, whichever machine it runs on. Yields False when another run holds it."""
    if lake is None:
        yield True
        return
    key = "state/wikipedia/lock.json"
    held = lake.create(key, {"since": time.time()})
    if not held and (lake.age_seconds(key) or 0) > LOCK_STALE_HOURS * 3600:
        lake.delete(key)
        held = lake.create(key, {"since": time.time()})
    try:
        yield held
    finally:
        if held:
            lake.delete(key)


def plan_for(events: list[dict], lake: Lake | None, env: str, backfill: bool, today: date, window: int,
             titles: list[str] | None) -> list[dict]:
    """--title: those. The weekly run: events within `window` days. A backfill: every event not archived yet."""
    if titles:
        by_title = {e["title"]: e for e in events}
        return [by_title.get(t, {"title": t, "label": t, "event_date": None, "listed_in": None}) for t in titles]
    lo, hi = (today - timedelta(days=window)).isoformat(), (today + timedelta(days=window)).isoformat()
    recent = [e for e in events if e["event_date"] and lo <= e["event_date"] <= hi]
    if not backfill:
        return recent
    zones = ("",) if env == "prod" else ("", "dev/")
    have = archived_titles(lake, zones) if lake else set()
    return [e for e in events if e["title"] not in have or e in recent]


def run(env: str, run_id: str, lake: Lake | None, backfill: bool, interval: float, max_requests: int,
        max_minutes: float, window: int = WINDOW_DAYS, titles: list[str] | None = None) -> dict:
    """The list, then each planned event, archived as served; the rows parsed from what was archived."""
    fetch_date, run_dir = date.today().isoformat(), RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    budget = Budget(max_requests, max_minutes)
    pages, missing, anomalies, report, stopped = [], [], [], Counter(), None

    def archive(kind: str, title: str, body: bytes) -> Path:
        local = run_dir / "raw" / kind / f"{page_name(title)}.json.gz"
        local.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(local, "wb", compresslevel=6) as out:
            out.write(body)
        pages.append((local, raw_json_key(kind, fetch_date, title, env)))
        return local

    with wiki_lock(lake) as held:
        if not held:
            return {"fetch_date": fetch_date, "run_dir": str(run_dir), "pages": [], "rows": {TABLE: []},
                    "anomalies": [], "missing": [], "report": {}, "stopped": "another run holds the lock"}
        f = Fetcher(budget, interval)
        try:
            status, body = f.parse(LIST_PAGE)
            if status != "ok":
                raise Stop(f"{LIST_PAGE}: {status}")
            archive("list", LIST_PAGE, body)
            events = listed_events(json.loads(body)["parse"]["wikitext"])
            plan = plan_for(events, lake, env, backfill, date.today(), window, titles)
            log(f"{len(events)} events listed; {len(plan)} to read")
            for i, e in enumerate(plan, 1):
                status, body = f.parse(e["title"])
                if status == "missing":
                    missing.append(e["title"])
                    report["events_missing"] += 1
                    continue
                if body is None:
                    anomalies.append({"id": e["title"], "code": "fetch_failed", "detail": status})
                    continue
                archive("event", e["title"], body)            # as served, before parsing
                report["events_kept"] += 1
                if i % 50 == 0:
                    log(f"  {i}/{len(plan)} events read")
        except Stop as e:
            stopped = str(e)
            log(f"stopped: {e}")
    rows, parse_failures, counts = page_rows(pages)
    anomalies += parse_failures
    report.update(counts, requests=budget.spent, rows=len(rows))
    log(f"{budget.spent} requests; {len(pages)} pages archived; {len(rows):,} change rows")
    return {"fetch_date": fetch_date, "run_dir": str(run_dir), "pages": pages, "rows": {TABLE: rows},
            "anomalies": anomalies, "missing": missing, "report": dict(report), "stopped": stopped}


def page_rows(pages: list[tuple[Path, str]]) -> tuple[list[dict], list[dict], Counter]:
    """Archived pages -> change rows, parse failures, counts. The crawl and a replay both
    come through here, so a replay gives exactly what the crawl did. The list among the
    pages gives each event its date."""
    rows, anomalies, counts, listed = [], [], Counter(), {}
    for local, key in pages:
        if "/wikipedia/list/" in key:
            try:
                for e in listed_events(json.loads(gzip.decompress(local.read_bytes()))["parse"]["wikitext"]):
                    listed[e["title"]] = e
            except (OSError, ValueError, KeyError) as ex:
                anomalies.append({"id": key, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
    for local, key in pages:
        if "/wikipedia/event/" not in key:
            continue
        requested = page_title(local.name[: -len(".json.gz")])
        fetch_date = key.split("/fetch_date=", 1)[1].split("/", 1)[0]
        try:
            parsed = json.loads(gzip.decompress(local.read_bytes()))["parse"]
            changes, c = background_changes(parsed["wikitext"], parsed["title"])
        except (OSError, ValueError, KeyError) as ex:
            anomalies.append({"id": requested, "code": "parse_failed", "detail": f"{type(ex).__name__}: {ex}"})
            continue
        counts.update(c)
        counts["events_parsed"] += 1
        ev = listed.get(requested, {})
        for r in changes:
            rows.append({
                "page_title": parsed["title"], "requested_title": requested, "page_id": parsed.get("pageid"),
                "revid": parsed["revid"], "event_label": ev.get("label"), "event_date": ev.get("event_date"),
                **r,
                "source_url": f"https://en.wikipedia.org/w/index.php?oldid={parsed['revid']}", "license": LICENSE,
                "_raw_html_key": key, "fetch_date": fetch_date,
            })
    return rows, anomalies, counts


def replay(lake: Lake, env: str, run_id: str) -> dict:
    """Re-parse the latest archived copy of every page in this environment's zone, straight
    from the bucket. No requests to Wikipedia: a parser change is replayed, never re-crawled."""
    latest: dict[tuple[str, str], str] = {}
    prefix = f"{zone(env)}raw-json/wikipedia/"
    for blob in lake.bucket.list_blobs(prefix=prefix):
        if blob.name.endswith(".json.gz"):
            kind, name = blob.name[len(prefix):].split("/", 1)[0], blob.name.rsplit("/", 1)[1]
            if blob.name > latest.get((kind, name), ""):          # fetch_date sorts as text
                latest[(kind, name)] = blob.name
    run_dir, pages = RUNS / run_id, []
    for (kind, name), key in sorted(latest.items()):
        local = run_dir / "raw" / kind / name
        local.parent.mkdir(parents=True, exist_ok=True)
        lake.bucket.blob(key).download_to_filename(str(local))
        pages.append((local, key))
    rows, anomalies, counts = page_rows(pages)
    log(f"replay: {len(pages)} pages in {prefix} -> {len(rows):,} change rows")
    return {"fetch_date": date.today().isoformat(), "run_dir": str(run_dir), "pages": pages, "rows": {TABLE: rows},
            "anomalies": anomalies, "missing": [], "report": {**counts, "pages_replayed": len(pages),
            "rows": len(rows)}, "stopped": None, "replay": True}


def upload(lake: Lake, result: dict, staged: dict, run_dir: Path, env: str) -> tuple[int, int]:
    """Raw JSON first, then the Parquet, so no row points at an unarchived page. A page
    the API says does not exist is recorded as a marker, so a backfill does not ask again."""
    for local, key in [] if result.get("replay") else result["pages"]:
        lake.upload_file(local, key, "application/gzip")
    for title in result.get("missing", []):
        lake.write_json(raw_json_key("event", result["fetch_date"], title, env).replace(".json.gz", ".missing.json"),
                        {"error": "missingtitle"})
    files = [f for _, fs in staged.values() for f in fs]
    for f in files:
        lake.upload_file(f, parsed_key(f, run_dir, env), "application/octet-stream")
    return len(result["pages"]), len(files)


def verify(result: dict, staged: dict, run_dir: Path, env: str, lake: Lake | None) -> dict:
    """Every archived page is valid gzip and, once uploaded, the same size in the bucket;
    every one parsed; every row carries its revision; the Parquet holds exactly the parsed rows."""
    fails, warns = [], []
    for local, key in result["pages"]:
        try:
            gzip.decompress(local.read_bytes())
        except OSError:
            fails.append(f"corrupt gzip: {local}")
        if lake and lake.size(key) != local.stat().st_size:
            fails.append(f"raw page missing or wrong size in bucket: {key}")
    for a in result["anomalies"]:
        (fails if a["code"] == "parse_failed" else warns).append(f"{a['code']}: {a['id']} {a['detail'][:80]}")
    raw_keys = {k for _, k in result["pages"]}
    recs = result["rows"][TABLE]
    n = staged.get(TABLE, (0, []))[0]
    if n != len(recs):
        fails.append(f"{TABLE}: {len(recs)} rows parsed but {n} in Parquet")
    if {r["_raw_html_key"] for r in recs} - raw_keys:
        fails.append(f"{TABLE}: rows point at an unarchived page")
    if any(not r.get("revid") for r in recs):
        fails.append(f"{TABLE}: rows without a revision id cannot be attributed")
    if lake:
        for _, files in staged.values():
            for f in files:
                if lake.size(parsed_key(f, run_dir, env)) != f.stat().st_size:
                    fails.append(f"Parquet missing or wrong size in bucket: {parsed_key(f, run_dir, env)}")
    fails += roundtrip_mismatches(result["rows"], staged)
    if result["stopped"]:
        warns.append(result["stopped"])
    return {"fails": fails, "warns": warns}


def landing_count(run_id: str, env: str) -> int:
    cur = snowflake_cursor(env)
    cur.execute(f"select count(*) from FIGHTGRADE_{env.upper()}.LANDING.LND_{TABLE.upper()} "
                f"where _object_key like %s", (f"%part-{run_id}-%",))
    return cur.fetchone()[0]


def load_landing_cli(env: str) -> bool:
    from dbt.cli.main import dbtRunner

    res = dbtRunner().invoke(["build", "--select", DBT_SELECT, "--target", env,
                              "--project-dir", str(ROOT / "dbt"), "--profiles-dir", str(Path("~/.dbt").expanduser())])
    return bool(res.success)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true", help="every event not archived yet")
    ap.add_argument("--title", action="append", help="read only this event article (repeatable)")
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    ap.add_argument("--interval", type=float, default=MIN_INTERVAL)
    ap.add_argument("--max-requests", type=int, default=0,
                    help=f"default {WEEKLY_BUDGET}, or {BACKFILL_BUDGET} for a backfill")
    ap.add_argument("--max-minutes", type=float, default=MAX_FETCH_MINUTES)
    ap.add_argument("--dry-run", action="store_true", help="fetch and archive locally; upload nothing")
    ap.add_argument("--replay", action="store_true",
                    help="re-parse the latest archived copy of every page and load LANDING; no requests")
    args = ap.parse_args()
    if args.interval < MIN_INTERVAL:
        ap.error(f"--interval below {MIN_INTERVAL} s breaks the Wikipedia crawl's pace")

    env, run_id = environment(), new_run_id()
    run_dir = RUNS / run_id
    log(f"run {run_id} env={env} zone='{zone(env)}' -> {run_dir.relative_to(ROOT)}")
    lake = None if args.dry_run else Lake(env)
    if args.replay:
        result = replay(lake or Lake(env), env, run_id)
    else:
        result = run(env, run_id, lake, args.backfill, args.interval,
                     args.max_requests or (BACKFILL_BUDGET if args.backfill else WEEKLY_BUDGET), args.max_minutes,
                     args.window_days, args.title)
    shutil.rmtree(run_dir / "tables", ignore_errors=True)
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = stage(result["rows"], run_dir, run_id)
    if lake:
        n_pages, n_files = upload(lake, result, staged, run_dir, env)
        log(f"uploaded {0 if result.get('replay') else n_pages} pages and {n_files} Parquet files")
    checks = verify(result, staged, run_dir, env, lake)
    report = {"run_id": run_id, "env": env, "fetch_date": result["fetch_date"], "crawl": result["report"],
              "missing": result["missing"], **checks}
    if lake and not checks["fails"] and staged:
        report["dbt_success"] = load_landing_cli(env)
        n = staged[TABLE][0]
        report["landing"] = {"parquet": n, "landing": landing_count(run_id, env)}
        if report["landing"]["landing"] != n:
            report["fails"].append(f"LANDING {TABLE}: {report['landing']['landing']} rows loaded, {n} expected")
        if not report["dbt_success"]:
            report["fails"].append(f"dbt build --select {DBT_SELECT} failed")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, indent=1))
    log("report:\n" + json.dumps({**report, "warns": report["warns"][:20]}, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
