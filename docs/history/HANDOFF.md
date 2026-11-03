# Tapology scrape — HANDOFF

Last updated: 2026-08-22 19:25 EDT.

**Stage 3 is COMPLETE.** The full-archive re-parse is running now and finishes ~11 PM
tonight, unattended. Nothing needs a human until it lands.

---

## 1. Current state

| Stage | Status | Output |
|---|---|---|
| 1 · Promotion index | ✅ done | 7,007 promotions |
| 2 · Event index | ✅ done | 87,597 events (promotion-walk) |
| 2b · Sitemap index completion | ✅ done | +17,410 events the walk could not reach |
| 3 · Event details | ✅ **done** | **104,372 events, 821,040 bouts** |
| 3b · Full re-parse | 🔄 **running** | finishes ~23:00 EDT 2026-08-22 |
| 4 · Fighter pages | ❌ **not needed** — see §6 | — |
| 5 · Daily increment | 🅿️ parked, half-built | `driver/_parked/crawl_daily.WIP.py` |

### Numbers

```
events parsed        104,372      date range   1909-05-01 .. 2026-12-05
bouts                821,040      archive      104,397 pages, 4.7 GB gzip
fighter appearances  1,929,149    distinct fighter slugs  ~302,000
anomalies             31,765      (all understood — see §5)
```

Date precision: `full` 85,598 · `assumed_year` 1,364 · `from_page` 17,410.

### Locations
- **Everything lives in `~/Desktop/MMA Data`.** The old
  `~/ufc-elo-calculator/seeder_data/tapology_scrape` path is an empty skeleton — ignore it.
- `data/` outputs · `scrapers/` code · `logs/` state and audits · `samples/` reference HTML

---

## 2. The datasets

| file | rows | key |
|---|---|---|
| `data/events.jsonl` | 105,007 | `event_slug` — the worklist (stage 2 + sitemap) |
| `data/event_details.jsonl` | 104,372 | `event_slug` — one row per event |
| `data/bouts.jsonl` | 821,040 | `(event_slug, bout_id)` |
| `data/event_fighters.jsonl` | 1,929,149 | event ↔ fighter link rows, incl. cancelled bouts |
| `data/promotions.jsonl` | 7,007 | `id` |
| `data/promotions_detail.jsonl` | 7,000 | `promotion_id` |
| `data/html/<aa>/<bb>/<slug>.html.gz` | 104,397 | **the raw archive — see §4** |

Joins: `bouts.event_slug` → `event_details.event_slug` → `promotion.promotion_id` →
`promotions.id`. All verified 100% on crawled events.

### Fields worth knowing about
- **`fighter_slug` is the fighter identity.** There is no fighter id. Slugs take any
  form (`40148-islam-makhachev`, `134267`, `a-sol-kwan`) and cannot be derived from names.
- **`organisation` / `organisation_source`** — one grouping key for "who ran this".
  Falls back to Ownership when Tapology says `Promotion: N/A` (16,462 such events).
  `ownership` is separately useful: Zuffa spans UFC/WEC/Strikeforce.
- **`order` vs `order_derived` vs `order_effective`** — `order` is the slot on the
  *announced* card and can exceed the number of bouts shown (removed bouts leave gaps).
  `order_derived = N - card_position + 1`. `order_source` says which you have.
- **`sport_base` / `level` / `modified_rules`** — age grades (Kids/Juniors) and rule
  variants fold into the parent sport, matching Tapology's own counters.
- **`bout_counts` is counted from the card**, never trusted from the production block.
  `bout_counts_declared` keeps the page's own numbers; `bout_counts_agree` flags conflict.

---

## 3. Running things

```bash
cd ~/Desktop/MMA\ Data
python3 scrapers/driver/status3.py     # progress snapshot
tail -20 logs/audit.log                # 45-min integrity + coverage audits
cat logs/after_crawl.log               # re-parse chain; says ALL DONE when finished
tail -f logs/crawl3.log                # live crawl log
```

Start a crawl (only needed if new events appear):
```bash
caffeinate -dimsu &
nohup bash scrapers/driver/supervise3.sh --sleep 1.0 --rotate-every 450 > logs/supervisor3.stdout 2>&1 &
nohup bash scrapers/driver/audit_loop.sh > /dev/null 2>&1 &
```

Stop everything:
```bash
pkill -f supervise3.sh; pkill -TERM -f crawl_stage3.py
pkill -f poll3.py; pkill -f audit_loop.sh; pkill -f 'caffeinate -dimsu'
```

**Correct restart order is supervisor first, then crawler** — otherwise the supervisor
relaunches the crawler and you end up with two. A PID lock now prevents that anyway.

---

## 4. The archive is the point

`data/html/` holds every page gzipped (4.7 GB, 6.9× compression). **Re-parsing the whole
corpus takes ~3.5 h with no network.** Crawling it took 26 hours through Cloudflare.

So: **never re-crawl to fix a parser bug.** Fix the extractor, run
`python3 scrapers/driver/reparse.py --commit`, done. It rebuilds all three tables and
regenerates `anomalies.jsonl` from scratch, and refuses to commit if the rebuild has
fewer events or bouts than the current data.

This mattered — 11 parser bugs were found *during* the crawl and all of them are fixed
retroactively by the run happening now.

---

## 5. Anomalies (31,765) — all understood

| count | code | meaning |
|---|---|---|
| 16,462 | `no_promotion` | Tapology genuinely files no promotion. 4,712 rescued via Ownership. |
| 10,144 | `zero_bouts_declared` | page says "No fight card available", or event cancelled |
| 4,040 | `completed_bout_no_result` | old records, no winner ever recorded |
| 882 | `fighter_without_slug` | fighter has no Tapology profile — name still captured |
| 173 | `counts_disagree` | **stale; drops to ~0 after the re-parse** |
| 31 | `fetch_failed` | stale; all were successfully re-crawled later |
| 23 + 10 | `zero_bouts_*` | cancelled events / legacy code |

**None indicate data loss.** Anything genuinely missing is missing from Tapology.

---

## 6. Why stage 4 (fighter pages) is NOT needed

Measured, not assumed:

- **Profile fields are rendered into every bout.** height, reach, nickname and DOB are
  100% all-or-nothing per fighter across 87,935 fighters with 3+ bouts. We already hold
  every value Tapology has. Reach at 10% is a **ceiling in the source**, not a crawl gap
  (verified: Peter Aerts and Yuki Kondo read `Reach: N/A` on their own pages).
- **DOB is derivable** from `age_at_fight` + event date, exact to the day. Verified
  against Khabib (1988-09-19) and Pereira (1987-07-07).
- **Completeness is handled by the sitemap** (§7), not by fighter pages.

The only things fighter pages uniquely add are **gym affiliation** and **"fighting out
of"**, both of which also appear on bout pages.

---

## 7. How the index was completed (and how to redo it)

Stage 2 walked promotion pages, so events belonging to **no promotion** were structurally
unreachable — 17,410 of them, including the entire pre-war era.

Two dead ends, recorded so nobody retries them:
- `/fightcenter?schedule=results` caps at ~15 pages. Recent results only, not an archive.
- **Bare event ids do not resolve.** `/fightcenter/events/97108` redirects to the
  homepage; the full slug is required.

**What works: `https://www.tapology.com/sitemap.xml`** — a sitemap index listing 476
sub-sitemaps: **46 event**, 252 bout, 130 fighter, 1 promotions. 46 requests enumerated
104,984 event slugs. Diffing against our index found the 17,410 gap.

To re-check for new events later, refetch the event sitemaps and diff. The bout and
fighter sitemaps offer the same completeness check for those stages.

---

## 8. Method (unchanged from stage 2, still the only thing that works)

Cloudflare requires **headed real Chrome**. `requests`, `curl_cffi` and headless
Playwright all fail. The browser is a credential holder: it clears the challenge once,
then every page is fetched by same-origin `fetch()` **inside** that page.

Tuned settings, measured over 104k pages:
- **1.0 s between events** — the throughput ceiling. Faster shrinks the per-IP budget
  faster than it gains (530 events/IP at 1.0 s vs 225 at 0.7 s, same ~37 ev/min).
- **Rotate IP every 450 events**, and **immediately on any 503** — a 503 is a per-IP
  limit and rotation clears it in ~6 s. Waiting does nothing.
- **43-region LRU pool** from 104 candidates (`logs/vpn_candidates.txt`). Regions that
  fail to clear Cloudflare twice retire themselves and are replaced. 11 retired so far.
- Disposable temp Chrome profile per launch. Never reuse a profile.

Traps that cost real time:
1. **"Temporarily Unavailable"** — Tapology serves a tiny error page with a normal
   response. Not a Cloudflare challenge. It silently poisoned 450 events before being
   caught. Both a fetch-time and a parse-time guard now reject it.
2. **Never advance the worklist on a soft failure.** The first version treated 503 as a
   permanent miss and sprinted through the queue hammering a rate-limited server.
3. **Never run two crawlers.** They race on the archive temp file and duplicate rows.

---

## 9. Watch it yourself

Two detached loops, independent of any Claude session:

- `scrapers/driver/audit_loop.sh` → `logs/audit.log` every 45 min
  - `selfcheck.py`: processes alive, progress, **new anomaly codes**, **archived pages
    lacking event markup**, duplicate rows, checkpoint agreement, torn JSON, VPN pool
  - `coverage_audit.py`: per-sport × per-field fill rates; flags a field that is zero for
    one sport but healthy for that sport historically, or that drops sharply
- `scrapers/driver/poll3.py` → `logs/monitor3.log` heartbeat

The coverage audit found **3 real parser bugs** the integrity check could not: dropped
round formats (`3-3-3 +OT`), the no-contest `N` marker (3,595 bouts), and weight-class
pills (`HW`). Keep it running for future crawls.

---

## 10. Backlog

1. **Title-fight bout pages** — 59,977 pages, ~27 h. Adds `Title on Line` and
   `Belt Status Before Fight` (present in *every* sport, verified across 8), plus gym and
   "fighting out of". Filter on `title_kind == "title"` — **not** `"tournament"`, or you
   pull in 12,511 IBJJF/ADCC bracket rounds that carry no belt data.
   **Full instructions: `docs/stage3b-title-bouts.md`.**
2. **Derived `replacement` / short-notice flag** — 62,978 cases where a fighter appears
   in both a cancelled and a completed bout on the same card. Reason text sometimes names
   it outright (`Moicano to Main Event`). Notice *length* is not recoverable.
3. **Stage 5 daily increment** — unblocked now. Needs a launchd LaunchAgent in the GUI
   session (headed Chrome; cron/server will not work). Also picks up the ~635 future-dated
   events deliberately skipped.
4. **Promotion strength model** — the original goal. `docs/promotion_strength.md`.

---

## 11. Gotchas

- Mac must stay awake and logged in; `caffeinate -dimsu`, lid open. Screen lock is fine.
- The crawler's Chrome uses a temp profile, so clicking the dock icon may surface *its*
  window rather than your signed-in one. Your profile is never touched.
- Recent index rows omit the year (`"Saturday, August 15, 5:30 PM ET"`) — the year comes
  from the event page.
- `remaining` never reaches 0: future-dated events are never crawled. Completion is the
  `logs/stage3.done` flag.
- 27 distinct bout sports exist, not the 11 an early survey suggested. Judo, Sambo,
  Taekwondo, Ice Fighting, Kun Khmer and Pancrase are all real, distinct values.
- Docs: `docs/stage3-extraction.md` is the field-by-field capture spec and variation log.
