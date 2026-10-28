# Tapology field census — capture EVERYTHING

Exhaustive inventory of every data element on each page type. The crawler captures
all of these verbatim; feature selection happens later, downstream — the scrape does
not drop anything.

Internal reference links are captured as `{id, slug}` (id may be null for legacy slugs,
e.g. fighter `a-sol-kwan`, event `affliction-1-banned`).

---

## 1. PROMOTION page  `/fightcenter/promotions/{id}-{slug}`

**Promotion Details**
- `name`
- `headquarters`
- `acronyms`
- `also_known_as`
- `logo_image_url`
- `promotion_links[]` — every external URL in the "Promotion Links" block
  (official site, Wikipedia, Facebook, X/Twitter, Instagram, YouTube, TikTok, …),
  each kept as `{platform, url}` (platform from host).

**Event List** — paginated `?page=N`; one row per `#preview{eventId}`:
- `event_id`, `event_slug`, `event_name`
- `date_time_full` (e.g. "Friday, December 11, 9:00 PM ET"), `date_time_short`
- `broadcast` (Amazon Prime / Internet PPV / YouTube / …)
- `venue_name`
- `city`, `country`
- `region_name`, `region_slug` (`/regions/{slug}`)
- `title_fight` (bool) + `title_name` (e.g. "Featherweight Title Fight")
- `main_event_matchup` (e.g. "Haggerty vs. Akimoto")
- `card_preview[]` (upcoming events expose per-bout previews): `{fighter_a, fighter_b, sport, weight}`
  — **sport tag here is a load-bearing signal** (MMA / Muay Thai / Kickboxing / Grappling / Boxing / Karate).
- `pagination` (current/last page)

---

## 2. EVENT page  `/fightcenter/events/{id}-{slug}`

**Event Production**
- `date_time` (e.g. "Saturday 09.03.2011 at 08:00 AM ET")
- `us_broadcast`
- `preliminary_card` (broadcast)
- `promotion` `{id, slug, name}`
- `ownership`
- `venue`
- `location` (`city`, `country`)
- `enclosure` (Cage / Ring)
- `tv_announcers`
- `post_fight_interviews`
- `bout_counts` — **per sport**: `MMA Bouts: N` (and `Muay Thai Bouts`, `Kickboxing Bouts`, … on mixed cards)
- `promotion_links[]`
- `event_links[]` — external URLs in "Event Links" block (Sherdog, BestFightOdds, Wikipedia, …)
- `poster_image_url`

**Fight Card** — one block per bout:
- `bout_order` (# 10 … 1), `bout_id`, `bout_slug` (from "Matchup Page" link)
- `result` (W/L), `winner_side`
- `method` (+ detail, e.g. "Decision, Unanimous"), `rounds`, `total_time`
- per fighter (A/B): `name`, `fighter_id`, `fighter_slug`, `record_after`
  ("Up to 10-1" / "Down to 16-8"), `nickname`
- `billing` (Main Event / Co-Main / …), `weight`, `round_format` (3 x 5), `pro_am` (Professional MMA)
- per fighter: `betting_odds`, `age_at_fight`, `weigh_in_result`, `height`, `reach`

---

## 3. BOUT page  `/fightcenter/bouts/{id}-{slug}`  (richest per-fight record)

**Header / result**
- `event` `{id, slug, name}`, `date`, `location`, `region_slug`
- `status` ("Completed MMA Bout" → status + sport + type)
- `winner`, per-fighter `W/L`
- `method` + detail, `rounds`, `total_time`
- `awards[]` (e.g. "Fight of the Night")
- `poster_image_url`

**Fighter Comparison** (per fighter A/B)
- `name`, `nickname`, `fighter_id`, `fighter_slug`
- `pro_record_at_fight`  ← record BEFORE (intake / matchmaking signal)
- `record_after_fight`
- `betting_odds`
- `nationality`
- `fighting_out_of`
- `age_at_fight`
- `weigh_in_result`
- `height`
- `reach`
- `gym` `{id, slug, name}`

**Fight Predictions**
- `community_picks_count`
- `method_distribution` (KO/TKO, Submission, Decision)
- per-fighter `pick_pct`

**Fight Details**
- `billing`, `weight_class`, `weight`, `pro_am`
- `event` `{id, slug, name}`, `date`, `referee`, `venue`, `enclosure`, `location`
- `bout_billing` (Main Event, "fight 10 of 10")
- `tv_commentary`, `broadcast`, `post_fight_interviewer`
- per-fighter `disclosed_pay`
- `event_links[]`
- `forum_thread` (Event Discussion link, or "no official forum thread")

---

## Notes
- Every page carries page-chrome social/share links and ad hosts — these are **excluded**;
  only links inside the labelled "Promotion Links" / "Event Links" blocks are captured.
- Responsive layout renders many values twice (desktop + mobile). Extractors dedupe.
- Sport/ruleset and pro/am are "Tapology editorial judgment" (per their disclaimer) —
  captured as-is, not re-derived.
