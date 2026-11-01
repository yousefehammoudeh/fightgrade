"""News headlines -> events, by a small local model: candidates -> the lake -> LANDING.

    .venv/bin/python -m pipeline.news_classify --eval                  # score models on the gold set
    .venv/bin/python -m pipeline.news_classify                         # DEV: classify new candidates
    .venv/bin/python -m pipeline.news_classify --limit 50 --dry-run

The model runs in Ollama (http://localhost:11434, or OLLAMA_HOST), on this
machine or inside the Dagster image, never a paid API. It reads one headline
(the slug as words, with its day and site) and answers in a fixed JSON schema:
event type, status, fighters, replaced fighter, opponent, event, reason. The
labels and their rules are experiments/news/LABELS.md; the gold set is the seed
news_event_labels.

Each answer is kept whole, as the model gave it, with the model and
PROMPT_VERSION that produced it: lnd_news_events. A headline is classified once
per (model, prompt version), so a run only reads candidates with no answer yet,
and a new prompt version re-reads them all. int_news_events keeps the answers
of the chosen model and version.
"""
import argparse, csv, json, os, re, shutil, sys, time, urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from .lake import Lake, environment, new_run_id, zone
from .tapology import parsed_key, roundtrip_mismatches, snowflake_cursor, stage

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "_crawlruns" / "news_classify"
GOLD = ROOT / "dbt" / "seeds" / "news_event_labels.csv"
TABLE = "news_events"
DBT_SELECT = "lnd_news_events"

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = "qwen3:4b"
MODELS = ("llama3.2:3b", "qwen3:4b", "gemma3:4b")
WORKERS = 4                 # requests in flight; Ollama serves OLLAMA_NUM_PARALLEL of them at once
TIMEOUT = 120

EVENT_TYPES = ("withdrawal", "replacement", "weight_miss", "retirement", "return", "suspension", "release",
               "signing", "fought_injured", "booking", "none")
STATUSES = ("confirmed", "speculative", "denied")
REASONS = ("injury", "illness", "weight_cut", "visa", "doping", "medical", "legal", "other")
# Headlines about another sport, whatever the model says (LABELS.md: not MMA is none). Kept beside
# the model's answer as `not_mma`, and applied in int_news_events, so a change here needs no re-run.
NOT_MMA = re.compile(r"(^| )(boxing|boxer|bkfc|bare knuckle|raf|glory \d+|glory (kickboxing|world series)|kickboxing|"
                     r"jiu ?jitsu|bjj|adcc|grappling|wwe|aew|pro wrestling|karate combat|nfl|nba)( |$)")
# ... unless the headline also names MMA or an MMA promotion ("former boxing champion set for mma
# debut", "chris leben staying retired unless jake paul needs an opponent ufc mma boxing").
IS_MMA = re.compile(r"(^| )(mma|ufc|bellator|pfl|strikeforce|wec|pride|one championship|rizin|lfa|invicta|"
                    r"cage warriors|ksw|wsof|elitexc|affliction|dream|sengoku|titan fc|brave cf)( |$)")


def not_mma(headline: str) -> bool:
    return bool(NOT_MMA.search(headline)) and not IS_MMA.search(headline)


# What the precision bar is set on (experiments/news/README.md, step 4).
BAR_TYPES = ("retirement", "withdrawal", "replacement")
BAR = 0.9

SCHEMA = {
    "type": "object",
    "properties": {
        "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
        "status": {"type": "string", "enum": list(STATUSES)},
        "fighters": {"type": "array", "items": {"type": "string"}},
        "replaced_fighter": {"type": "string"},
        "opponent": {"type": "string"},
        "event": {"type": "string"},
        "reason": {"type": "string", "enum": ["", *REASONS]},
    },
    "required": ["event_type", "status", "fighters", "replaced_fighter", "opponent", "event", "reason"],
}

PROMPT_VERSION = "v2"
# v2 (2026-10-31): the gold set's rulings (experiments/news/LABELS.md) written in, and
# examples that are invented headlines, never gold rows, so the gold set stays a test.
SYSTEM = """You label MMA news headlines. Each headline is an article's URL slug turned into words: \
lower case, no punctuation, apostrophes dropped. It may be cut off mid-sentence. Read only the headline.

event_type, the most specific that fits (replacement over withdrawal over booking):
- retirement: a fighter retires, is said to have retired, or fights a bout billed as their last \
("final fight", "last faceoff ever"). A reaction to someone's retirement carries it too.
- return: a fighter who had retired comes back (un-retires, a comeback fight after retiring). \
"Returns" after a layoff or to a promotion is booking.
- withdrawal: a fighter is out of a booked MMA bout (injury, illness, weight cut, visa, pregnancy), or a \
bout or card is cancelled or postponed, with no replacement. Injury news that does not say a bout is \
lost ("breaks hand in training", "injury update ahead of ufc 300") is a speculative withdrawal.
- replacement: a booked bout gets a new opponent because someone is out: "x steps in for y", \
"x replaces y", "x vs z booked with y injured", "x gets new opponent", "newcomer steps in on four days \
notice". replaced_fighter is who is out. A bout only talked about, never booked ("was set to face", \
"in talks"), is speculative.
- weight_miss: a fighter misses weight. Making weight, a vow not to miss, or a weigh-in show is none.
- suspension: a fighter is suspended or banned, or fails a drug test (reason doping); a commission's \
ban; a post-fight medical suspension (reason medical).
- release: a promotion cuts or releases a fighter, parts ways, or removes them from its roster.
- signing: a fighter signs or re-signs with a promotion, or wins a contract.
- fought_injured: a fighter reveals they fought a bout hurt.
- booking: an MMA bout is announced, set or matched ("draws", "to face", "returns at ufc 300 against").
- none: everything else: results, previews, weigh-in shows, interviews, call-outs, opinion, rankings, \
odds, videos, deaths, business. Also none whenever the sport is not MMA, even with MMA fighters: boxing \
(exhibitions too), bkfc (bare knuckle boxing), raf (wrestling), grappling, jiu jitsu, kickboxing, \
glory, karate combat, pro wrestling. Also none when the one replaced, retiring or released is not a \
fighter (a commentator, coach, referee, executive).

status: confirmed if the headline states it as fact; speculative for hints, considering, could, may, \
wants, plans, ponders, open to, rumours, reports of talks; denied if someone says it is not so \
("not retiring", "shoots down rumours"). Use confirmed for none.

fighters: whom the event happens to, names as written. opponent: the other fighter in the bout. \
event: the card if named ("ufc 254"). reason: why, if named. Empty string when not named; never take \
a name or a card from the examples."""


def _label(event_type, status="confirmed", fighters=(), replaced="", opponent="", event="", reason=""):
    return {"event_type": event_type, "status": status, "fighters": list(fighters), "replaced_fighter": replaced,
            "opponent": opponent, "event": event, "reason": reason}


# Invented headlines: no gold row, no real fight.
EXAMPLES = [
    ("tom brask announces retirement after ufc 900 loss to leo varga",
     _label("retirement", fighters=["tom brask"], opponent="leo varga", event="ufc 900")),
    ("ana pell hints she could retire after one more fight",
     _label("retirement", "speculative", ["ana pell"])),
    ("dana white says ivo marsh is not retiring despite rumours", _label("retirement", "denied", ["ivo marsh"])),
    ("ufc 901 weigh in results ivo marsh makes weight for final fight",
     _label("retirement", fighters=["ivo marsh"], event="ufc 901")),
    ("rafa kell injured out of ufc 902 bout against jon dray",
     _label("withdrawal", fighters=["rafa kell"], opponent="jon dray", event="ufc 902", reason="injury")),
    ("jon dray suffers broken foot in training", _label("withdrawal", "speculative", ["jon dray"], reason="injury")),
    ("ufc 903 sid moor vs pat lune booked with ken vale hurt",
     _label("replacement", fighters=["pat lune"], replaced="ken vale", opponent="sid moor", event="ufc 903",
            reason="injury")),
    ("ken vale was set to face pat lune before plans changed",
     _label("replacement", "speculative", ["ken vale"], opponent="pat lune")),
    ("boxer milo ruiz pulls out of title fight and gets new opponent", _label("none")),
    ("gil ortz pulled from bkfc 950 as opponent steps in", _label("none")),
    ("former champ replaces sam toll on ufc broadcast team", _label("none")),
    ("retired legend bo crane books ufc 904 comeback fight",
     _label("return", fighters=["bo crane"], event="ufc 904")),
    ("bo crane returns at ufc 905 against eli fenn",
     _label("booking", fighters=["bo crane"], opponent="eli fenn", event="ufc 905")),
    ("ufc releases three fighters including former title challenger", _label("release")),
    ("eli fenn tests positive for banned substance", _label("suspension", fighters=["eli fenn"], reason="doping")),
    ("ufc 906 poster released", _label("none", event="ufc 906")),
]


ARTICLE_PROMPT_VERSION = "a2"
# a1 (2026-10-31): the article, for headlines the slug pass flags (pipeline/news_articles.py).
# a2: the gold set's last-fight, withdrawal and replacement rules spelled out for articles.
ARTICLE_SYSTEM = SYSTEM.replace(
    "You label MMA news headlines. Each headline is an article's URL slug turned into words: lower case, no "
    "punctuation, apostrophes dropped. It may be cut off mid-sentence. Read only the headline.",
    "You label MMA news articles. You get the headline, the description and the start of the article. Label "
    "the event the headline is about, as the article reports it. The article settles who it happens to: a "
    "headline can name several fighters, and fighters lists only the one(s) the event happens to (for a "
    "retirement, whoever retires; a reaction or a statement by someone else does not make them retire). It "
    "settles fact or talk: a retirement announced for after a named, booked last fight is confirmed; a vague "
    "plan (\"in two or three years\", \"soon\", \"maybe\") is speculative; \"won't retire\" is denied. "
    "Whatever the headline's form (a weigh-in result, a staredown video, a card announcement), if the article "
    "says the bout is a fighter's last or retirement fight, the event is a confirmed retirement. A bout "
    "scratched, pulled, cancelled or postponed is a confirmed withdrawal, even when the reason is undisclosed "
    "or a new opponent is only being sought or called for. A fighter named as the replacement, stepping in or "
    "up on short notice, or taking a new opponent after a withdrawal is a replacement, not a booking. An injury "
    "or rumour that the article says is false is denied.")
ARTICLE_EXAMPLES = [
    ("Ivo Marsh reacts to Tom Brask's retirement", "Ivo Marsh says rival Tom Brask leaving MMA is a loss.",
     "Ivo Marsh paid tribute on Monday to Tom Brask, who announced his retirement after UFC 900.",
     _label("retirement", fighters=["tom brask"], event="ufc 900")),
    ("Ana Pell: 'I'll retire in a few years'", "The champion is thinking about the end.",
     "Ana Pell says she plans to fight on for two or three more years before she retires.",
     _label("retirement", "speculative", ["ana pell"])),
    ("Bo Crane will retire after Rafa Kell fight", "UFC 903 will be his last.",
     "Bo Crane confirmed Tuesday that his bout with Rafa Kell at UFC 903 will be the final fight of his career.",
     _label("retirement", fighters=["bo crane"], opponent="rafa kell", event="ufc 903")),
    ("Sid Moor's new opponent announced for UFC 904", "A newcomer steps in.",
     "Pat Lune will now face Sid Moor at UFC 904. Lune replaces Ken Vale, who withdrew with a knee injury.",
     _label("replacement", fighters=["pat lune"], replaced="ken vale", opponent="sid moor", event="ufc 904",
            reason="injury")),
]


ARTICLE_MIN_CHARS = 200     # a shorter body (an empty media-call page, an abridged 2011 post): keep the slug's answer


def article_not_mma(a: dict) -> bool:
    """The not-MMA filter over the article's own headline, description and opening."""
    return not_mma(" ".join(clean_words(x) for x in (a.get("headline"), a.get("description"),
                                                     (a.get("body") or "")[:400], a.get("section"))))


def clean_words(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower().replace("'", "").replace("\u2019", ""))


def article_message(a: dict) -> str:
    return (f"Headline: {a.get('headline') or ''}\nPublished: {a.get('date_published') or 'unknown'} on "
            f"{a.get('site') or 'unknown'}\nDescription: {a.get('description') or ''}\n"
            f"Article: {(a.get('body') or '')[:ARTICLE_CHARS]}")


ARTICLE_CHARS = 1500


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def user_message(headline: str, published_date: str | None, site: str | None) -> str:
    return f"Headline: {headline}\nPublished: {published_date or 'unknown'} on {site or 'unknown'}"


def messages(headline: str, published_date: str | None, site: str | None) -> list[dict]:
    out = [{"role": "system", "content": SYSTEM}]
    for h, label in EXAMPLES:
        out += [{"role": "user", "content": user_message(h, None, None)},
                {"role": "assistant", "content": json.dumps(label)}]
    return out + [{"role": "user", "content": user_message(headline, published_date, site)}]


def article_messages(a: dict) -> list[dict]:
    out = [{"role": "system", "content": ARTICLE_SYSTEM}]
    for h, d, b, label in ARTICLE_EXAMPLES:
        out += [{"role": "user", "content": article_message({"headline": h, "description": d, "body": b})},
                {"role": "assistant", "content": json.dumps(label)}]
    return out + [{"role": "user", "content": article_message(a)}]


def ask(model: str, headline: str, published_date: str | None = None, site: str | None = None,
        article: dict | None = None) -> dict:
    """One headline, or with `article` one article -> {'answer': parsed JSON or None, 'raw': the
    model's text, 'seconds'}. A model named '<name>+think' lets a reasoning model think first."""
    think = model.endswith("+think")
    body = {"model": model.removesuffix("+think"), "stream": False, "format": SCHEMA, "think": think,
            "options": {"temperature": 0, "seed": 0, "num_predict": 2000 if think else 200},
            "messages": article_messages(article) if article else messages(headline, published_date, site)}
    req = urllib.request.Request(f"{OLLAMA}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = json.loads(r.read())["message"]["content"]
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:
        answer = None
    return {"answer": answer, "raw": raw, "seconds": round(time.time() - t0, 3)}


def classify(rows: list[dict], model: str, workers: int = WORKERS, articles: bool = False) -> list[dict]:
    """rows: {news_url_id, headline, published_date, site} -> one answer row each, in order.
    With articles, each row also carries its article (headline, description, body,
    date_published) and is read by the article prompt."""
    version = ARTICLE_PROMPT_VERSION if articles else PROMPT_VERSION

    def one(r):
        try:
            got = ask(model, r["headline"], r.get("published_date"), r.get("site"), r if articles else None)
        except OSError as e:
            got = {"answer": None, "raw": f"ERROR {type(e).__name__}: {e}", "seconds": None}
        a = got["answer"] or {}
        return {"news_url_id": r["news_url_id"], "model": model, "prompt_version": version,
                "classified_on": date.today().isoformat(), "event_type": a.get("event_type"),
                "status": a.get("status"), "fighters": a.get("fighters") or [],
                "replaced_fighter": a.get("replaced_fighter") or None, "opponent": a.get("opponent") or None,
                "event": a.get("event") or None, "reason": a.get("reason") or None,
                "not_mma": article_not_mma(r) if articles else not_mma(r["headline"]),
                "raw": got["raw"], "seconds": got["seconds"]}
    t0, out = time.time(), []
    with ThreadPoolExecutor(workers) as pool:
        for i, row in enumerate(pool.map(one, rows), 1):
            out.append(row)
            if i % 500 == 0:
                log(f"  {model}: {i:,} of {len(rows):,} ({(time.time() - t0) / i:.2f} s each)")
    return out


# ---------------------------------------------------------------- evaluation

def read_gold(path: Path = GOLD) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def key(event_type: str | None, status: str | None) -> str:
    return f"{status}_{event_type}" if event_type and event_type != "none" else "none"


def score(gold: list[dict], pred: dict[str, dict], candidates_only: bool = True) -> dict:
    """Per (status, type): precision, recall, support. With candidates_only, a headline the
    keyword pass does not mark (now, not when it was sampled) counts as predicted none, as in
    production."""
    tp, fp, fn = Counter(), Counter(), Counter()
    for g in gold:
        p = pred.get(g["news_url_id"], {})
        if (candidates_only and not g["is_candidate"]) or not_mma(g["headline"]) or p.get("not_mma"):
            p = {}
        want, got = key(g["event_type"], g["status"]), key(p.get("event_type"), p.get("status"))
        if want == got:
            tp[want] += 1
        else:
            fp[got] += 1
            fn[want] += 1
    out = {}
    for k in sorted(set(tp) | set(fp) | set(fn)):
        if k == "none":
            continue
        pr = tp[k] / (tp[k] + fp[k]) if tp[k] + fp[k] else None
        rc = tp[k] / (tp[k] + fn[k]) if tp[k] + fn[k] else None
        out[k] = {"precision": pr, "recall": rc, "support": tp[k] + fn[k], "predicted": tp[k] + fp[k]}
    return out


def bar_summary(scores: dict) -> dict:
    """The README's bar: precision >= BAR on confirmed retirements, withdrawals, replacements."""
    rows = {t: scores.get(f"confirmed_{t}", {}) for t in BAR_TYPES}
    return {t: {k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()} for t, r in rows.items()} | {
        "meets_bar": all((r.get("precision") or 0) >= BAR for r in rows.values())}


def by_group(gold: list[dict], pred: dict[str, dict], group) -> dict:
    groups = defaultdict(list)
    for g in gold:
        groups[group(g)].append(g)
    return {k: bar_summary(score(v, pred)) for k, v in sorted(groups.items())}


def current_candidates(env: str) -> set[str]:
    """The gold headlines today's keyword pass marks."""
    cur = snowflake_cursor(env)
    cur.execute(f"select h.news_url_id from FIGHTGRADE_{env.upper()}.REFINED.STG_NEWS_HEADLINES h "
                f"join FIGHTGRADE_{env.upper()}.LANDING.NEWS_EVENT_LABELS g using (news_url_id) where h.is_candidate")
    return {r[0] for r in cur.fetchall()}


def half(news_url_id: str) -> str:
    """'tune' or 'test', fixed by the id: prompts are tuned on errors in one half only."""
    import hashlib
    return "test" if int(hashlib.md5(news_url_id.encode()).hexdigest(), 16) % 2 else "tune"


def evaluate(models: list[str], workers: int, out_dir: Path, env: str = "dev") -> dict:
    marked = current_candidates(env)
    gold = [{**g, "is_candidate": g["news_url_id"] in marked} for g in read_gold() if g["event_type"]]
    log(f"gold set: {len(gold)} labelled headlines, {len(marked)} marked by the keyword pass")
    report = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for m in models:
        t0 = time.time()
        rows = classify([{"news_url_id": g["news_url_id"], "headline": g["headline"], "published_date": g["published_date"],
                          "site": g["site"]} for g in gold], m, workers)
        secs = time.time() - t0
        (out_dir / f"{m.replace(':', '_')}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        pred = {r["news_url_id"]: r for r in rows}
        decade = lambda g: f"{g['published_date'][:3]}0s"
        report[m] = {"seconds_per_headline": round(secs / len(gold), 3),
                     "unparsed": sum(r["event_type"] is None for r in rows),
                     "bar": bar_summary(score(gold, pred)),
                     "bar_by_half": by_group(gold, pred, lambda g: half(g["news_url_id"])),
                     "bar_by_decade": by_group(gold, pred, decade),
                     "all_types": {k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                   for k, v in score(gold, pred).items()},
                     "classifier_alone": bar_summary(score(gold, pred, candidates_only=False))}
        log(f"{m}: {json.dumps(report[m]['bar'])}")
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    return report


FLAG_TYPES = ("retirement", "return", "withdrawal", "replacement")    # what the slug pass sends to the article pass


def flagged(answer: dict) -> bool:
    return answer.get("event_type") in FLAG_TYPES and not answer.get("not_mma")


def gold_articles(env: str) -> dict[str, dict]:
    """The newest parsed article for each gold headline, from LANDING."""
    cur = snowflake_cursor(env)
    db = f"FIGHTGRADE_{env.upper()}"
    cur.execute(f"""select payload from {db}.LANDING.LND_NEWS_ARTICLES
                    where payload:news_url_id::varchar in (select news_url_id from {db}.LANDING.NEWS_EVENT_LABELS)
                    qualify row_number() over (partition by payload:news_url_id order by _loaded_at desc) = 1""")
    return {a["news_url_id"]: a for a in (json.loads(r[0]) for r in cur.fetchall())}


def evaluate_pipeline(model: str, slug_answers: Path, workers: int, out_dir: Path, env: str = "dev") -> dict:
    """Both stages on the gold set: the slug answers in `slug_answers` (an --eval run's JSONL
    for this model), then the article pass on every flagged headline whose page was read.
    The article's answer replaces the slug's; every other headline keeps the slug's."""
    marked = current_candidates(env)
    gold = [{**g, "is_candidate": g["news_url_id"] in marked} for g in read_gold()]
    slug = {json.loads(l)["news_url_id"]: json.loads(l) for l in slug_answers.read_text().splitlines() if l}
    for a in slug.values():
        a.setdefault("not_mma", not_mma(next((g["headline"] for g in gold if g["news_url_id"] == a["news_url_id"]), "")))
    pages = gold_articles(env)
    todo = [{**pages[i], "headline": pages[i]["headline"] or "", "site": pages[i]["site"]}
            for i, a in slug.items() if flagged(a) and i in pages and (pages[i]["body_chars"] or 0) >= ARTICLE_MIN_CHARS]
    log(f"{sum(flagged(a) for a in slug.values())} gold headlines flagged by the slug pass, {len(todo)} with a page read")
    t0 = time.time()
    rows = classify(todo, model, workers, articles=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{model.replace(':', '_')}_articles.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    final = {**slug, **{r["news_url_id"]: ({**r, "event_type": "none"} if r["not_mma"] else r)
                        for r in rows if r["event_type"]}}
    random400 = [g for g in gold if not g["stratum"].startswith("ext_")]
    report = {"seconds_per_article": round((time.time() - t0) / max(len(rows), 1), 2), "articles": len(rows),
              "random400": bar_summary(score(random400, final)), "all600": bar_summary(score(gold, final)),
              "slug_only_random400": bar_summary(score(random400, slug)), "slug_only_all600": bar_summary(score(gold, slug)),
              "all_types_600": {k: {kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()}
                                for k, v in score(gold, final).items()}}
    (out_dir / "pipeline_report.json").write_text(json.dumps(report, indent=1))
    return report


# ---------------------------------------------------------------- production

PENDING = """
select h.news_url_id, h.headline, h.published_date::varchar, h.site
from FIGHTGRADE_{ENV}.REFINED.STG_NEWS_HEADLINES h
where h.is_candidate
  and not exists (select 1 from FIGHTGRADE_{ENV}.LANDING.LND_NEWS_EVENTS e
                  where e.payload:news_url_id::varchar = h.news_url_id
                    and e.payload:model::varchar = %s and e.payload:prompt_version::varchar = %s)
order by h.published_date desc
"""


def pending(env: str, model: str) -> list[dict]:
    from .news_sitemaps import ensure_dev_tables
    ensure_dev_tables(env)
    cur = snowflake_cursor(env)
    cur.execute(f"select count(*) from FIGHTGRADE_{env.upper()}.INFORMATION_SCHEMA.TABLES "
                f"where table_schema = 'LANDING' and table_name = 'LND_NEWS_EVENTS'")
    sql = PENDING.replace("{ENV}", env.upper())
    if not cur.fetchone()[0]:                          # first run: nothing classified yet
        sql = sql.split("  and not exists")[0] + "order by h.published_date desc"
        cur.execute(sql)
    else:
        cur.execute(sql, (model, PROMPT_VERSION))
    return [dict(zip(("news_url_id", "headline", "published_date", "site"), r)) for r in cur.fetchall()]


PENDING_ARTICLES = """
select a.payload, h.headline as slug_headline
from {DB}.LANDING.LND_NEWS_ARTICLES a
join {DB}.REFINED.STG_NEWS_HEADLINES h on h.news_url_id = a.payload:news_url_id::varchar
where a.payload:body_chars::int >= %s
  and not exists (select 1 from {DB}.LANDING.LND_NEWS_EVENTS e
                  where e.payload:news_url_id::varchar = a.payload:news_url_id::varchar
                    and e.payload:model::varchar = %s and e.payload:prompt_version::varchar = %s)
qualify row_number() over (partition by a.payload:news_url_id order by a._loaded_at desc) = 1
order by a.payload:date_published::varchar desc
"""


def pending_articles(env: str, model: str) -> list[dict]:
    from .news_sitemaps import ensure_dev_tables
    ensure_dev_tables(env)
    cur = snowflake_cursor(env)
    cur.execute(PENDING_ARTICLES.replace("{DB}", f"FIGHTGRADE_{env.upper()}"), (ARTICLE_MIN_CHARS, model, ARTICLE_PROMPT_VERSION))
    return [{**json.loads(p), "slug_headline": h} for p, h in cur.fetchall()]


def run(env: str, run_id: str, model: str, limit: int, workers: int, articles: bool = False) -> dict:
    if articles:
        todo = pending_articles(env, model)[: limit or None]
        log(f"{len(todo):,} articles to classify with {model} {ARTICLE_PROMPT_VERSION}")
        rows = classify(todo, model, workers, articles=True)
        today = date.today().isoformat()
        for r in rows:
            r["_raw_html_key"], r["fetch_date"] = None, today
        return {"run_dir": str(RUNS / run_id), "rows": {TABLE: rows},
                "report": dict(Counter(r["event_type"] or "unparsed" for r in rows), classified=len(rows))}
    todo = pending(env, model)
    if limit:
        todo = todo[:limit]
    log(f"{len(todo):,} candidates to classify with {model} {PROMPT_VERSION}")
    rows = classify(todo, model, workers)
    today = date.today().isoformat()
    for r in rows:
        r["_raw_html_key"], r["fetch_date"] = None, today
    report = Counter(r["event_type"] or "unparsed" for r in rows)
    return {"run_dir": str(RUNS / run_id), "rows": {TABLE: rows}, "report": dict(report, classified=len(rows))}


def upload(lake: Lake, staged: dict, run_dir: Path, env: str) -> int:
    files = [f for _, fs in staged.values() for f in fs]
    for f in files:
        lake.upload_file(f, parsed_key(f, run_dir, env), "application/octet-stream")
    return len(files)


def check_lake(lake: Lake, result: dict, staged: dict, run_dir: Path, env: str) -> list[str]:
    fails = [f"Parquet missing or wrong size in bucket: {parsed_key(f, run_dir, env)}"
             for _, files in staged.values() for f in files
             if lake.size(parsed_key(f, run_dir, env)) != f.stat().st_size]
    n = staged.get(TABLE, (0, []))[0]
    if n != len(result["rows"][TABLE]):
        fails.append(f"{TABLE}: {len(result['rows'][TABLE])} rows classified but {n} in Parquet")
    return fails + roundtrip_mismatches(result["rows"], staged)


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
    ap.add_argument("--eval", action="store_true", help="score the models on the gold set; nothing is uploaded")
    ap.add_argument("--eval-articles", type=Path, metavar="SLUG_JSONL",
                    help="score both stages on the gold set, from an --eval run's slug answers for --model")
    ap.add_argument("--model", action="append", help=f"default: {MODEL}; with --eval, all of {', '.join(MODELS)}")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--articles", action="store_true",
                    help=f"classify the articles read (lnd_news_articles) with prompt {ARTICLE_PROMPT_VERSION}, not the slugs")
    ap.add_argument("--workers", type=int, default=WORKERS)
    ap.add_argument("--dry-run", action="store_true", help="classify and stage locally; upload nothing")
    args = ap.parse_args()

    run_id = new_run_id()
    if args.eval_articles:
        report = evaluate_pipeline((args.model or [MODEL])[0], args.eval_articles, args.workers,
                                   RUNS / f"eval-articles-{run_id}", environment())
        print(json.dumps({k: v for k, v in report.items() if k != "all_types_600"}, indent=1))
        return 0
    if args.eval:
        report = evaluate(args.model or list(MODELS), args.workers, RUNS / f"eval-{run_id}", environment())
        print(json.dumps({m: {"bar": r["bar"], "seconds_per_headline": r["seconds_per_headline"],
                              "unparsed": r["unparsed"]} for m, r in report.items()}, indent=1))
        return 0

    env = environment()
    model = (args.model or [MODEL])[0]
    run_dir = RUNS / run_id
    log(f"run {run_id} env={env} zone='{zone(env)}' model={model} "
        f"prompt={ARTICLE_PROMPT_VERSION if args.articles else PROMPT_VERSION}")
    result = run(env, run_id, model, args.limit, args.workers, args.articles)
    shutil.rmtree(run_dir / "tables", ignore_errors=True)
    shutil.rmtree(run_dir / "parsed", ignore_errors=True)
    staged = stage(result["rows"], run_dir, run_id)
    report = {"run_id": run_id, "env": env, "model": model,
              "prompt_version": ARTICLE_PROMPT_VERSION if args.articles else PROMPT_VERSION, **result["report"],
              "fails": []}
    if not args.dry_run and staged:
        lake = Lake(env)
        upload(lake, staged, run_dir, env)
        report["fails"] = check_lake(lake, result, staged, run_dir, env)
        if not report["fails"]:
            report["dbt_success"] = load_landing_cli(env)
            n, got = staged[TABLE][0], landing_count(run_id, env)
            report["landing"] = {"parquet": n, "landing": got}
            if got != n:
                report["fails"].append(f"LANDING {TABLE}: {got} rows loaded, {n} expected")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, indent=1))
    log("report:\n" + json.dumps(report, indent=1))
    return 1 if report["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
