# MMA news — bout disruptions, retirements and suspensions from headlines

> **Parked idea, 2026-10-28: not scheduled.** Revisit after the Nov 5 deep dive.
> What the warehouse cannot see today: **when** a bout changed (days of
> notice), **why** it was cancelled for the 81% of Tapology's cancelled bouts with no reason,
> and fighter events that are not bouts at all (retirement, return, suspension, release).
> Feeds the Elo short-notice test, the rankings' retired status and the fighter page.

## What exploration found (2026-10-28, ~15 polite requests)

| Source | Access | Archive | Notes |
|---|---|---|---|
| **MMA Fighting** (Vox) | robots.txt blocks Claude agents; **owners OK'd URLs, and article pages where necessary** | 245 monthly sitemaps, `/sitemaps/entries/YYYY/M`, **Dec 2003 →** | ~450–550 articles a month |
| **MMA Mania** (Vox) | same permission | ~250 monthly sitemaps, same layout | |
| MMA Junkie (Gannett) | HTTP 402 to non-browser clients | — | Out of scope unless Gannett OKs automated access |
| Bloody Elbow, MMA Weekly | robots allow general crawlers; WordPress / news sitemaps | not measured | Candidates for later |
| Sherdog | robots allow; sitemap behind a bot wall | — | Not pursued |

- **Every URL carries its publication day** (`/2020/10/24/21532169/khabib-nurmagomedov-announces-retirement-…`):
  1,006 of 1,006 in the two months checked. `<lastmod>` is the last edit, not publication.
  The exact time is only in the article's `published_time` meta tag.
- **Slugs carry the event**: of ~1,000 headlines, ~10–15% are disruptions, retirements, weight
  misses or suspensions. Both test cases were found from URLs alone: Khabib 2020-10-24, Jones
  2025-06-21, with the follow-ups (doubts, "hints at retirement", Dana White disputing the date).
- **Keywords alone misfire** (`ufc-255-poster-released`, `…octagon-return…`,
  `promises-to-not-miss-weight`, `open-to-serving-as-possible-replacement`) → a small model
  separates confirmed from speculative and extracts who, whom, which event and why.
- **Tapology already holds the structure**: 132,684 cancelled bouts on 39,565 events, 25,489
  (19%) with a reason. A replacement is derivable from it (X vs Y cancelled, Y fought Z on the
  card). The news supplies the date and the missing reasons.

## Rules

Honest user agent naming the project (`FightGrade-news/1.0 (private, non-commercial MMA research)`;
no contact, by choice); ≥ 6 s between requests per site; one lock per
site, as `pipeline/tapology.py` does; raw responses archived before parsing. Sitemaps only, plus
article pages solely for step 6's fight-week timestamps, keeping nothing from a page but
`published_time`. No browser, no user-agent spoofing. Any new site needs its own permission.

## Build plan

Each step lands as its own PR. Steps 1–4 do not depend on the Elo plan; 5–7 need its step 2
(Tapology bouts in REFINED).

**1. Sitemap crawl (1 day).** `pipeline/news_sitemaps.py` + Dagster job `news_crawl`.
- Backfill: every monthly sitemap of both sites, ~500 requests, < 1 h.
- Daily (05:00 Toronto, after the 02:00 Tapology run): the current and previous month, 4 requests.
- Raw XML to `raw-xml/news/<site>/fetch_date=<d>/YYYY-MM.xml.gz` (each read kept: a month's sitemap grows); Parquet rows `{site, url, article_id,
  url_date, slug, lastmod, fetch_date}` → LANDING `lnd_news_urls`.
- DEV run first: does Vox serve Dagster's AWS egress IPs? If not, the backfill runs from the
  laptop and only the daily job needs the cloud.

**2. Headlines in REFINED (½ day).** `stg_news_headlines`: dedupe on `(site, article_id)`, slug
→ words, `url_date`. A deliberately loose keyword pass marks candidates (~15%); its recall is
measured in step 3, so nothing it drops is unmeasured.

**3. Gold set (½ day, mostly review).** ~400 slugs, stratified: candidates and non-candidates,
2005 / 2015 / 2025. I draft labels, you review. Seed `news_event_labels.csv`: event type,
status, fighters, replaced fighter, opponent, event, reason.

**4. Classifier (1 day).** `int_news_events`, incremental, so each slug is classified once.
Cortex `AI_COMPLETE` with a JSON schema on a small model:

```json
{"event_type": "withdrawal|replacement|weight_miss|retirement|return|suspension|release|signing|fought_injured|booking|none",
 "status": "confirmed|speculative|denied",
 "fighters": ["..."], "replaced_fighter": "...", "opponent": "...", "event": "...", "reason": "..."}
```
- Two or three models compared on the gold set; the prompt is versioned with the output.
- Bar: precision ≥ 0.9 on confirmed withdrawals, replacements and retirements; report recall.
- Check first which small models Cortex serves in this account's region, and the credit cost on
  the ~30k candidates (~250k headlines in all).
- **Checked 2026-10-31: none.** The account (AWS_US_EAST_2) is a trial: `AI_COMPLETE` and
  `AI_CLASSIFY` answer "not available for trial accounts", and the legacy `SNOWFLAKE.CORTEX.*`
  functions are blocked for new accounts.

**5. Linking (1 day, after Elo step 2).** `xref_news_fighter` (names → `dim_fighter`, the same
accent-, case- and nickname-tolerant matching as the bout matcher) and `xref_news_bout`
(fighters + event name + date window → `fct_bout` and Tapology's cancelled bouts). Match rates
published, like `xref_decision_ufcstats_fight_match_rate`.

**6. Facts (1 day).**
- `fct_bout_disruption`: bout, withdrawn fighter, replacement, reason (news, else Tapology),
  first report date, **days_notice** = event date − first report.
- Fight-week precision: for disruptions reported ≤ 7 days out, fetch the article page once and
  keep only `published_time` (a few thousand pages over 20 years).
- `fct_fighter_status_event`: retirement / return / suspension / release, each a dated claim with
  its source. Current status is derived from the sequence (a return after a retirement wins).

**7. Use it (1 day).**
- Elo: add `short_notice` (the replacement, and the fighter who got a new opponent, on ≤ 14 days)
  to `factors.py`; it goes in only if it beats the 2020–24 test like the others.
- Rankings: a confirmed retirement removes a fighter at once; a return restores them.
- Fighter page: the disruption and status timeline, linked to the headlines.

## How we know it works

- Precision and recall on the gold set, per event type and decade.
- Every replacement the model finds is checked against Tapology's cancelled-bout structure.
- Retirements checked against a hand list (Khabib, Jones, GSP, Cormier, Nunes, …).
- Coverage per year and promotion, published: Vox coverage is UFC-heavy, regional cards thin,
  so absence of a headline is not absence of a disruption.

## Open decisions

1. Add Bloody Elbow and MMA Weekly (robots allow) for the years and promotions Vox covers thinly?
2. Ask Gannett about MMA Junkie, or leave it out?
3. Who reviews the gold set, and how many slugs is enough?
4. Before or after the Nov 5 deep dive? The Elo plan already fills most of the next week.
