# Data fixes applied post-scrape

Manual corrections to scraped data. Each entry records what was wrong, how it was
detected, the fix, and how to verify/revert.

---

## 2026-08-25 — 4 UFC events misfiled under "Road to UFC" (`promotion_id` 4353 → 1)

**Symptom.** `belts.jsonl` reports 26 bouts for the men's UFC featherweight belt
(`b4e2c2e62446deb`), but filtering title bouts via
`events.jsonl.promotion_id == 1` returned only 25. The missing bout was UFC 325.

**Root cause — not a scraper bug.** Tapology's *Road to UFC* promotion index
legitimately cross-lists UFC cards that contain Road to UFC tournament finals on
the prelims. The crawler records the index it found an event on
(`index_promotion_id`), so those four cards were attributed to promotion 4353
rather than promotion 1. Confirmed in the `raw_row_text` of each: every one has a
Road to UFC finalist bout on the card (Jubli/Saragih, Chang Ho Lee/Long Xiao,
Su Young You/Baergeng Jieleyisi, Dom Mar Fan/Sang Uk Kim).

**Detection method (authoritative).** `event_details.jsonl` carries two independent
promotion signals:
- `promotion.promotion_id` — scraped from the **event's own page** (ground truth)
- `index_promotion_id` — the promotion index the crawler found it on

Diffing them across all 86,987 events where both are non-null gave **exactly 4
disagreements**, all `4353 → 1`. No other promotion pair disagreed anywhere in the
dataset. `events.jsonl.promotion_id` matched `index_promotion_id` in all 86,987
cases, so `events.jsonl` was the only file needing correction.

Name-similarity heuristics were tried first and are useless for this — acronym
collisions (20 promotions share "EFC") and legitimate regional sub-promotions
("Shooto Europe" under "Shooto") produce ~5,000 false positives.

**Fix.** Set `promotion_id: 4353 → 1` on these four rows in `data/events.jsonl`:

| event_slug | event_name |
|---|---|
| `93977-ufc-fight-night` | UFC Fight Night: Lewis vs. Spivac |
| `109963-ufc-fight-night` | UFC Fight Night: Whittaker vs. Aliskerov |
| `116899-ufc-fight-night` | UFC Fight Night: Yan vs. Figueiredo |
| `136549-ufc-325-volkanovski-vs-lopes-2` | UFC 325: Volkanovski vs. Lopes 2 |

No other field touched; all other lines written back byte-identical. Written
atomically via temp file + `os.replace`.

**Blast radius.** None beyond `events.jsonl`. `bouts.jsonl`, `bout_details.jsonl`,
`bout_belts.jsonl`, `event_fighters.jsonl` and `belt_reigns.jsonl` carry no
promotion field; no belt in `belts.jsonl` is owned by `pid:4353`.

**`index_promotion_id` was deliberately left unchanged** in `event_details.jsonl`.
It is a provenance record of where the crawler found the event, not a claim about
who ran it. As a result `events.jsonl.promotion_id != index_promotion_id` for these
four rows by design — that is the intended post-fix state, and re-running the
detection above will still surface them.

**Verification.**
- 105,007 rows in / 105,007 out, 0 corrupt.
- UFC event count 809 → 813; Road to UFC 31 → 27 (27 genuine `Road to UFC` events remain).
- UFC title bouts at 145 lb via promotion filter: 32 → **33**, now agreeing with the
  belt-ownership route.
- Backup: `data/events.jsonl.pre-promofix-20260825.bak` (pre-fix, byte-identical
  except the 4 rows). Revert = restore that file.

---

## 2026-08-25 — tournament placement added to `bout_details.jsonl`

**Gap.** Stage 3b captured belt status but discarded tournament placement. Tapology
renders `Tournament:` and `Tournament Round:` in the same label/value list as
`Belt Status Before Fight:`, so `parsers.tapology.parse_bout_page` was **already
returning them** in `info` — `crawl_stage3b.flatten()` simply never wrote them out.

**Fix, no re-crawl.** Exactly the scenario the HTML archive exists for. Two lines
added to `flatten()`, then the current extractor replayed over `data/html_bouts/`
(2.2 GB, 46,893 pages) in ~140 s. The extractor itself needed no change.

Added `pipeline/tapology_archive/reparse3b.py` — the stage-3b counterpart to `reparse.py`,
which only covered stage 3 (events). Same contract: no network, `*.rebuilt.jsonl`
staged first, committed only after checks pass.

**New fields:** `tournament` (e.g. `"UFC 16 Lightweight"`), `tournament_round`
(`Finals` 591, `Semi-Finals` 25, `Quarter-Finals` 15, `Round of 16` 1).

**Verification.** 46,893 rows in/out, only `tournament` + `tournament_round` added,
**zero drift** on every pre-existing field. Backup:
`data/bout_details.jsonl.pre-reparse3b.bak`.

**Two things learned the hard way, both now guarded in `reparse3b.py`:**
- `parse_bout_page(html)` must be called **with the URL** (`parse_bout_page(html, url)`)
  or it silently nulls `bout_url`/`bout_id`/`bout_slug`. A first pass shipped
  `bout_url: null` on all 46,893 rows; caught only because the file got *smaller*.
- The drift guard originally checked a hand-picked field list, which is what let that
  through. It now diffs **every** pre-existing key and refuses to commit on any change
  outside the newly added ones.

**A field that was tried and dropped:** `belt_status_label_present`, intended to
separate "page says N/A" from "label missing" in `belt_status_kind: "absent"`.
Measured across all 46,893 rows it is constant `True`, and `no_belt_status_label`
has never fired in the anomaly log. So on this corpus **`"absent"` always means the
page explicitly printed N/A** — i.e. no belt existed going into that fight. The flag
carried no information and was removed rather than shipped as a dead column.

---

## 2026-08-25 — sitemap stub rows backfilled in `events.jsonl`

**Gap.** 17,410 rows carried only `event_slug`/`event_id`/`source: "sitemap"`. These
events appear on no promotion index page, so the stage-2 index crawl never built a
row — but stage 3 scraped their pages in full. The data was one file over the whole
time. A join, not a re-crawl. Script: `scripts/backfill_sitemap_events.py --commit`.

**Filled** from `event_details.jsonl`: `event_name` (17,410), `region_slug` +
`region_name` (16,051), `promotion_id` (948, all pids validated against
`promotions.jsonl`). `source` rewritten to `sitemap+detail_backfill` so backfilled
rows stay identifiable and the script is idempotent.

**Deliberately not filled.** `raw_row_text` is an index-row artefact these events have
no equivalent of. `title_fight`/`title_name` likewise — deriving `title_fight` from
`bouts.jsonl` was tested against all 87,597 known-good rows and agreed only **43.3%**
of the time (49,618 disagreements), so deriving it would have injected ~50k wrong
values. Left null; query `bouts.jsonl` directly instead.

**Verification.** 105,007 rows in/out, row order preserved, **zero drift on non-stub
rows**, only the 5 intended fields changed. Every event in the dataset now has a name
(105,007/105,007). Backup: `data/events.jsonl.pre-backfill.bak`.

**What is still null, and why it is not fixable:**
- **16,462 without `promotion_id`** — their Tapology page links no promotion at all.
  Mostly independent one-off shows; 6,764 name a promoter only as free text in the
  production block (see `event_promoter_freetext.jsonl`). Null is the correct value.
- **1,359 without `region_slug`** — two causes. (a) ~794 are in sub-Saharan Africa
  outside South Africa; Tapology's 27-region taxonomy has only a "South Africa
  Region", so there is no region for Tanzania (221), Nigeria (124), DRC (74), Malawi
  (45) etc. to link to. (b) ~565 are territories the taxonomy covers but the page did
  not tag, heavily skewed to **cancelled** events (450 of the 1,359 are Cancelled) plus
  small territories like Hong Kong, Cayman Islands and Suriname. Assigning these would
  mean inventing a region the source does not assert.

---

## 2026-08-25 — cancelled events removed

**What.** All 3,263 events with `event_details.status == "Cancelled"` deleted, plus
everything hanging off them. Status lives only in `event_details.jsonl`;
`events.jsonl` has no status field. Script:
`scripts/remove_cancelled_events.py --commit`.

| file | kept | dropped |
|---|---:|---:|
| events.jsonl | 101,744 | 3,263 |
| event_details.jsonl | 101,134 | 3,263 |
| bouts.jsonl | 820,880 | 166 |
| event_fighters.jsonl | 1,906,201 | 22,960 |
| bout_details.jsonl | 46,889 | 4 |
| event_promoter_freetext.jsonl | 5,078 | 187 |

Removed rows are **quarantined** in `data/removed_cancelled/`, not just backed up, so
any subset can be reinstated without a crawl. Per-file `.pre-cancelfix.bak` alongside.

**Derived tables were regenerated, not hand-edited.** `bout_belts.jsonl`,
`belts.jsonl` and `belt_reigns.jsonl` come from `derive/belts.py` then
`belt_lineage.py`. Before deleting anything, both were re-run on untouched data and
their output was **byte-identical** to the committed files, proving the derive is
reproducible and that any post-deletion change is attributable to the deletion alone.
After: belts 19,864 -> 19,860, belt-links 58,682 -> 58,674 (the 8 title bouts that sat
on cancelled cards, and 4 belts that existed only there).

Spot-check unchanged: UFC title bouts still 54 at 170 lb (incl. BMF) and 33 at 145 lb.

**CAVEAT — 88 of the 166 dropped bouts carried real results** (`W`/`L`, methods like
"KO/TKO, Punches", "Submission, Armbar"), which contradicts their event being
Cancelled — most likely partly-cancelled cards, or Tapology relabelling after the
fact. They were removed as instructed; they are recoverable from
`data/removed_cancelled/bouts.jsonl` if that subset should be reinstated.

---

## 2026-08-25 — six new region buckets; region backfilled from location

**Problem.** 2,170 events had no `region_slug` because their page carried no region
link. Tapology's taxonomy is 27 regions with **no general Africa bucket** -- only
"South Africa Region" -- so most of sub-Saharan Africa had nothing to link to.
Script: `scripts/assign_missing_regions.py --commit`.

**Rule 1, precedent first.** Where already-assigned events in the same country used a
region, that region was reused even where it looks geographically odd, because joins
key on the slug and consistency beats tidiness:
Puerto Rico -> `caribbean` (94 precedent), Hong Kong -> `china` (10),
Bermuda -> `united-kingdom-ireland` (6, British Overseas Territory),
Guam -> `southeast-asia` (23, Tapology's own grouping),
Egypt -> `middle-east` (227, so Egypt is NOT in the new africa-north),
District of Columbia -> `us-northeast` (from Maryland 121 / Delaware 89).

**Rule 2, new buckets only where nothing existing fits.** Six added, in the
taxonomy's own "<Area> Region" style:

| slug | name | events |
|---|---|---:|
| `africa-west` | Africa West Region | 599 |
| `africa-east` | Africa East Region | 479 |
| `africa-central` | Africa Central Region | 237 |
| `africa-southern` | Africa Southern Region | 173 |
| `africa-north` | Africa North Region | 37 |
| `oceania-pacific` | Oceania Pacific Region | 67 |

Africa is split four ways (plus the pre-existing `south-africa`) to match the
granularity the taxonomy already uses for Europe and Asia. Grouping is UN M49 except
that Zambia/Malawi/Zimbabwe/Mozambique are placed in `africa-southern` rather than
Eastern Africa, to sit alongside the existing `south-africa`.

**Result.** 2,088 events assigned; 27 regions -> 33; unassigned 2,170 -> **82**.
Touched rows carry `region_source: "derived_from_location"` so derived regions stay
separable from scraped ones. Zero drift on already-assigned rows. Backup:
`data/events.jsonl.pre-region.bak`.

**The 82 left null** are 71 US events whose location is literally "United States"/
"USA" with no state (a `us-*` sub-region cannot be chosen), 10 with no location at
all, and 1 "Andaman Sea". Guessing these would mean inventing a sub-region the source
does not assert.

---

## Known, NOT fixed

- **17,410 sitemap-only stub rows** in `events.jsonl` carry only `event_slug`,
  `event_id` and `source: "sitemap"`; every other field is `null`.

  **CORRECTION (2026-08-25): these are NOT a coverage gap and need no re-crawl.**
  An earlier revision of this file said they did — that was wrong. All 17,410 are
  fully present in `event_details.jsonl` with `parse_ok: true`, event names, dates,
  venues, referees and bout counts, and 14,989 of them have complete fight cards in
  `bouts.jsonl` (82,490 bouts). The stub rows exist because these events are not
  listed on any promotion's index page, so the stage-2 index crawl never produced an
  events.jsonl row for them — the stage-3 page crawl picked them up anyway.

  Two separate things are conflated in that null `promotion_id`:
  - **948 events whose page names a real linked promotion** (all 948 pids valid;
    ISCF 255, FEMFP 158, Shooto 138, ORP 100, …). These are straightforwardly
    backfillable from `event_details.jsonl.promotion.promotion_id`.
  - **~16.5k with no linked promotion on Tapology at all** — independent one-off
    shows, mostly boxing. 6,764 name a promoter only as free text in the production
    block; 5,265 already have a row in `event_promoter_freetext.jsonl`. For these
    `promotion_id` is correctly null and no edit can invent one.

  16,051 of the 17,410 have `event_name` + `region_slug` + `event_date_index` and
  could have a full `events.jsonl` row reconstructed from the detail file.

  Impact: 4,536 are MMA events and 1,801 Muay Thai, so any promotion-filtered query
  silently omits them. Prefer joining through `event_details.jsonl`.
- **5 rows** in `events.jsonl` have a null `event_id` and a bare slug
  (`91265`, `astra-yoshidas-farewell`, `moosin-god-of-martial-arts`,
  `wfa-4-king-of-the-streets`, `yarennoka-new-years-eve-2007`).
