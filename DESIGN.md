# FightGrade — MMA data platform

**Build window:** 2026-10-24 → 2026-11-05 (technical deep dive, Nov 5)
**Revision 2** — rewritten after reading `HANDOFF.md`, `HANDOFF.stage2.md` and all 19 `docs/`.
Revision 1 is in the scratchpad; three of its assumptions were wrong (§12).

**Two products off one pipeline:**
1. **Judge Lens** (UFC) — how would *this specific judge* have scored this fight, and would a
   different judge have changed the result?
2. **Résumé Strength** (all pro MMA) — fight records lie; quantify *who a fighter actually beat*,
   era-accurately, using the already-validated promotion-strength model.

---

## 1. Stack

| Concern | Tool | Note |
|---|---|---|
| Orchestration | **Dagster** | Airflow in the previous role; chosen deliberately — §7 |
| Infrastructure | **Terraform** | GCP + Snowflake account/schema/role/grant objects |
| Lake / raw archive | **GCS** (`us-central1`) | Immutable raw HTML + Parquet — §4. Was S3 until the AWS account was closed — §11 |
| Warehouse | **Snowflake** | LANDING → CLEANSING → REFINED → PUBLISHED |
| Modelling | **dbt** | Tables and views inside the layers, nothing else |
| ML | **Snowflake ML** (Snowpark + Experiments + Model Registry) | SQL-callable. ClearML cut — §8 |
| Serving | **Streamlit in Snowflake** | |
| Scraping | Playwright (Tapology, exists) + 1 new static scraper | |

---

## 2. Sources — two of three are already scraped

| Source | Location | Volume | Freshness | Work needed |
|---|---|---|---|---|
| **Tapology** | `~/Desktop/MMA Data/data/` | 820,880 bouts · 101,744 events · 7,007 promotions · 467,263 fighters | latest completed event **2026-08-22** | 62-day gap crawl |
| **UFC Stats** | `~/ufc-elo-calculator/seeder_data/data/` | 11,093 fights · **48,664 round rows** · 70,194 position + 70,194 target rows | fetched **2026-02/03** | ~7-month incremental |
| **MMADecisions** | — | judge scorecards | — | **the only new scraper** |

### UFC Stats is already round-level

`ufcstats_fight_rounds.csv` is keyed `(ufcstats_fight_id, ufcstats_fighter_id, round)` and carries
`kd, sig_str_land/att, total_str_land/att, td_land/att, sub_att, rev, ctrl`. Position
(distance/clinch/ground) and target (head/body/leg) are separate files at the same grain.
**That is the entire feature side of the judge model, already on disk.**

### Entity resolution is already solved for Tapology ↔ UFC Stats

| map | rows | coverage |
|---|---|---|
| `bout_id_map.csv` → `(ufcstats_fight_id, tapology_bout_id)` | 11,093 | **100% of UFC Stats fights** |
| `fighter_id_map.csv` → `(ufcstats_fighter_id, tapology_fighter_id)` | 4,046 | full Tapology slug, not the leading number |

Revision 1 called cross-source matching the project's biggest risk. For two of the three sources
it is done. The only matching left is **MMADecisions → `ufcstats_fight_id`**, a two-way match
against a closed universe of 11,093 fights with known dates and fighter names.

### Sizing

| | |
|---|---|
| All bouts | 820,880 |
| `sport_base = 'MMA'` | 517,655 |
| **Professional MMA, standard rules (core scope)** | **258,246** |
| MMA events | 55,504 |
| UFC events / pro bouts / **decisions** | 797 / 8,853 / **4,037** |
| Judge-round rows at full scorecard coverage | **~38,800** (budget 25–30k usable; MMADecisions thins pre-2008) |
| Raw HTML archive | **170,678 pages, 8.8 GB** gzipped |

---

## 2.5 What is already on disk, and how it is partitioned

The raw archive exists, gzipped and sha1-sharded. Uploading it is a migration, not a job.

| asset | volume | size |
|---|---|---|
| `data/html/` — event + promotion pages | 104,397 files | 5.7 GB |
| `data/html_bouts/` — bout detail pages | 66,281 files | 3.1 GB |
| **total raw HTML** | **170,678 `.html.gz`** | **8.51 GiB** |
| live JSONL (20 datasets, excl. `.bak`) | | 2.7 GB |
| `.bak` snapshots — **not uploaded** | | 2.8 GB |

Coverage: **event pages complete** (104,397 archived vs 101,134 parsed event_details). **Bout
pages deliberately partial** — 66,281 against 820,880 bouts, because bout detail was always
gap-fill; the 820k bouts come from parsing event cards. Not a gap.

### fetch_date partitions, from file mtime

```
entity  fetch_date      files        entity  fetch_date      files
event   2026-08-20      9,476        bout    2026-08-22      3,693
event   2026-08-21     52,320        bout    2026-08-23     33,356
event   2026-08-22     42,601        bout    2026-08-24      9,844
                                     bout    2026-08-26      8,101   <- the cancelled-title crawl
                                     bout    2026-08-27     11,287   <- the second gap crawl
```

mtime is trustworthy here because the last two buckets match the documented crawl sizes
**exactly** (8,101 cancelled title bouts, 11,287 remaining gap). `event_details.scraped_at` gives
the same window independently.

### The upload mechanism

`scripts/stage_archive_for_s3.py` — dry-run by default, `--commit` to build. The archive is
sha1-sharded, which gives no way to replay one fetch date, so the script builds a tree of
**hardlinks** in the target partition layout (zero extra disk, same filesystem) and leaves the
transfer to `gcloud storage rsync`, which is parallel, idempotent and resumable
for free. At 170k objects a mid-run failure must not restart from zero. It refuses to run on a key
collision.

One object per page, deliberately: 170k objects is nothing to the store, and random access to a
single page is what makes "re-parse one event" cheap — the entire reason the archive exists.
Tarring per partition would save object count and destroy that.

### Dual-write during the build

The crawler writes the archive locally today (`data/html/<aa>/<bb>/<slug>.html.gz`) and
`reparse.py` / `reparse3b.py` read those local paths. For the build window, **write both** — local
and object store. Disk is free, the replay demo stays fast, and rewriting the re-parse scripts to
stream from the cloud is avoidable risk in 12 days.

Production answer, worth naming: the object store is the archive, local is a cache, and the
re-parse reads by partition from the store. The key-layout change from sha1-shard to
`source/entity/fetch_date` is what makes partition-scoped replay possible at all.

---

## 3. Scope boundary — MMA only

One predicate, applied once, in REFINED:

```sql
sport_base = 'MMA' AND level = 'Professional' AND modified_rules = false
```

Amateur MMA (254,304 bouts) is loaded and typed in CLEANSING but excluded from REFINED facts.
Amateur bouts belong to a record but must never be pooled into opposition quality — and
`promotion_strength` already scores the two levels on **separate tracks** (LFA is 0.94
professional, 0.40 amateur), so mixing them would discard that work. `MMA (Bare Knuckle)` (239
bouts) is excluded as a distinct ruleset.

### The trap this scope must not fall into

This is documented, not hypothetical: `promotion_strength.md` v1 filtered `sport == 'mma'` and
thereby **erased Karate Combat (~1% MMA) and understated ONE Championship (~18% MMA)**. v2 fixed
it by making sport part of the key — ONE scores 0.93 MMA / 0.95 Muay Thai / 0.90 kickboxing /
0.89 grappling, and "one number cannot say all four."

So: `dim_promotion` carries `mma_share` and the full `sport_mix`, the metric is named **MMA
strength** and never "promotion strength", and promotion names are never used to infer sport
(~56% are unclassifiable).

---

## 4. What the object store is for

> **Provider:** GCS, not S3 — see §11. Every argument in this section is about object storage and
> none of it depends on the vendor; only the paths and the ingest mechanism changed in the port.


### 4.1 The raw HTML archive — already proven, not theoretical

170,678 pages are archived gzipped at `data/html/` (104,397 event pages, 5.7 GB) and
`data/html_bouts/` (66,281 bout pages, 3.1 GB), sha1-sharded two levels deep.

**The replay path is already implemented.** `scrapers/driver/reparse.py` and `reparse3b.py`
rebuild the parsed tables from the archive with **no network**: ~3.5 h for the full event corpus
against **26 h to crawl it**. Both stage `*.rebuilt.jsonl` and refuse to commit if the rebuild has
fewer rows, and the stage-3b drift guard diffs **every pre-existing key** and refuses to commit on
any change outside the newly added ones.

The track record is in `data-fixes.md`:
- **11 parser bugs** found *during* the crawl, all fixed retroactively by one re-parse.
- Tournament placement added as two lines in `flatten()`, then **46,893 pages replayed in 140
  seconds**. Zero drift on every pre-existing field.
- The guard earned its keep: a first pass called `parse_bout_page(html)` without the URL and
  shipped `bout_url: null` on all 46,893 rows. It was caught **only because the file got
  smaller** — which is why the guard now diffs every key rather than a hand-picked list.

So the deep-dive demo is a live parser change backfilled across the full history with zero
requests to Tapology. **That is the central claim of the architecture, and it is already true.**

Migration is `gcloud storage rsync` plus a key transform from sha1-shard to source/entity/date layout:

```
gs://fightgrade-lake/raw-html/source=tapology/entity=event/fetch_date=2026-08-22/<slug>.html.gz
gs://fightgrade-lake/parsed/entity=bouts/fetch_date=2026-08-22/part-*.parquet
```

Lifecycle: raw HTML → Nearline after 90 days (instant reads, no restore step, so replay is unaffected).

### 4.2 Landing-zone staging
Parquet → GCS → external stage → `COPY INTO` asset in Dagster → `LANDING`. Snowpipe auto-ingest
(GCS notification → Pub/Sub → pipe) is available, but a daily partitioned load does not need it.

### 4.3 Secrets and observability
**Secret Manager** (Snowflake key-pair auth), **Cloud Logging + a budget alert** (crawler logs,
failure and spend alarms).

### 4.4 Can the crawler run in the cloud? Tested, and the answer changed

`HANDOFF.stage2.md` §3 concluded Cloudflare needs **headed real Chrome on a logged-in macOS
desktop**, which would make the crawler permanently un-containerisable. That table has an
**untested cell**: only *headless* Chromium and *headed real Chrome* were ever measured — never
**headed bundled Chromium**.

Tested it on 2026-10-24. Harness and full write-up: `experiments/xvfb-probe/`.

**Setup.** `python:3.12-slim` + Xvfb 1920x1080x24 + Playwright bundled Chromium,
`headless=False`, linux/arm64 native. Real Google Chrome was unavailable — `playwright install
chrome` has **no arm64 build** — which made it a clean test of Chromium on its own.

**Result — it clears.**

```
seed nav: HTTP 200   cf-mitigated=None   title='Arctic Combat 12 | Event | Tapology'   282,422b
same-origin fetch() through the cleared page, 1.0s pacing:
  HTTP 200  264,492b  CLEARED      HTTP 200  690,989b  CLEARED      HTTP 200  476,561b  CLEARED
```

The documented working primitive — clear once in a real browser, then `fetch()` every subsequent
page same-origin inside it — works unchanged in the container. And `navigator.webdriver` was
**`true`**: the automation flag was plainly visible and it cleared anyway, which fits the docs'
own measurement that the limit is **soft** (transient 503s under load) rather than a bot ban.

**Two corrections to the original conclusion:**
1. **Real Google Chrome is not required** — bundled Chromium clears it when *headed*. Headed-ness
   is the load-bearing property, not the branding.
2. **macOS is not required** — Linux + Xvfb clears it, so a GUI desktop session is not the
   constraint either.

The original finding that *headless* never clears stands and is not contradicted; headless and
headed-under-Xvfb present differently, and only the former was tested.

### The one variable still open: datacenter IP reputation

The probe ran from a **residential IP**. Cloudflare scores cloud-provider ranges far more harshly, so
this isolates the browser-fingerprint question and says it passes — it says nothing about IP
reputation.

| test | cost | what it settles |
|---|---|---|
| **GitHub Actions `ubuntu-latest`** | free, Azure datacenter IPs | the cheapest datacenter-IP test; no new account |
| GCE e2-micro in `us-central1` | free (always-free tier) | the actual target environment |

**If it passes:** the crawler is containerisable, the DAG runs fully in the cloud, and the local
edge agent, launchd and GUI session all disappear.
**If it fails:** the blocker is **IP reputation, not the browser** — and the fix is a
residential/ISP proxy or a browser-as-a-service provider (Browserbase, Brightdata, Steel), not a
different browser configuration.

**Plan stance:** build Phase 0–3 against the **local** edge agent, because it is proven and needs
no new dependency. Run the datacenter-IP test in Phase 1 once the GCP project exists. Promote the
crawler into the cloud only if that passes and there is slack — it is an upgrade, not a
prerequisite. Either way the architecture is now evidence-based, and *"I tested whether the
scraper could be cloudified, here is the experiment"* is a better deep-dive answer than either
assumption.

### What volume *does* change: almost all of the operational apparatus

The original crawl was 104k pages over 26 hours. The daily increment is not, and the machinery
built for the former is unnecessary for the latter. Measured over 232 days of 2026:

| | all-sport events/day | MMA events/day |
|---|---|---|
| mean | 24.4 | 10.9 |
| median | 6 | 6 |
| p90 | 83 | 52 |
| **Saturday mean** | **108.9** | **52.2** |
| **worst day in 8 months** | **169** | 86 |

Worst-case run — a peak Saturday, all sports, landing everything unfiltered:

```
169 event pages  +  ~99 title-bout pages  =  ~268 pages
  at the measured 38 pages/min            =  ~7 minutes
  against the measured per-IP budget      =  51% of ONE IP
```

A median day is ~10 pages and under 20 seconds.

**So three dependencies drop out of the daily job:**

| apparatus | why it existed | daily increment |
|---|---|---|
| **ExpressVPN CLI + 43-region LRU pool, rotate every 450** | the per-IP budget ran out mid-crawl | **drop it.** A peak day uses half of one IP. Replace with retry + backoff on 503; keep rotation as a manual escape hatch, not scheduled behaviour |
| **`caffeinate -dimsu`, lid open** | a 26-hour crawl dies if the Mac sleeps | **irrelevant** for a 7-minute run. The Mac only has to be awake *at* 02:00 — see below |
| **supervisor + crash-loop backoff + PID lock** | hours of unattended running | keep the single-instance lock (cheap, prevents the archive race); the supervisor is overkill |

### The partition is the contract, not the schedule

If the Mac is asleep at 02:00 the run does not happen — and with **date-partitioned assets that
does not matter**. The partition is simply unmaterialized, and the next run backfills it. No
missed-cron recovery logic, no `pmset` wake schedule required, no alerting on a skipped night.

That is a genuine argument for partitioned assets over cron, and worth saying out loud: *the
schedule is best-effort; the partition is the contract.*

### Where Dagster runs

**One local Dagster instance** on the Mac (launchd-started daemon), materializing the three edge
assets with headed Chrome, writing to GCS, then running dbt against Snowflake. Simplest thing that
works, and the volume numbers above say it is not a compromise.

The production shape, worth naming without building: **edge agent pushes to GCS, a GCS sensor in a
cloud Dagster deployment picks it up, and the cloud deployment owns everything from LANDING
down.** Two deployments, one asset graph. For a single-machine project that split buys nothing.

### PROD is what main has: the two jobs, on Dagster+

| job | does | when |
|---|---|---|
| `dbt_prod_build` | `dbt build --target prod` from main | daily 03:00 Toronto; or launched by hand (optional `select`) — the old `dbt_manual_build` |
| `dbt_resync_dev` | `resync_dev_from_prod`: clone PROD over DEV, drop DEV-only tables | weekly, Sunday 04:00, after that night's build |

**Where they run: Dagster+ Serverless** (moved 2026-10-26). Every push to `main` redeploys the
code location from GitHub Actions, and the dbt project ships *inside* the package
(`pyproject.toml` force-includes `dbt/`), so a deployed location carries exactly the models of the
commit it was built from — the deploy is what makes PROD = main. Credentials are Dagster+
deployment environment variables, read by `orchestration/dbt_profiles/profiles.yml`; nothing on a
laptop is involved, and `DBT_PROD_SVC_PRIVATE_KEY` is scoped to the full deployment only.

**Pull requests get branch deployments.** There `dbt_prod_build` builds DEV, not PROD — unmerged
code never reaches PROD, and without the PROD key it couldn't authenticate if it tried. Schedules
run only on the production deployment.

**Locally** (`orchestration/run_dagster.sh`) the same jobs still work for development: they fetch
`origin/main` into `~/.fightgrade/main`, hard-reset and `git clean -fdx` it, and run dbt with
`~/.dbt/profiles.yml`. Local schedules start stopped, so a laptop never races Dagster+ for the
same 03:00 build. dbt runs in-process through `dbtRunner` in both places, because a packaged code
location has no `dbt` console script on its path.

These are op jobs. Phase 3 replaces the build step with `dagster-dbt` assets alongside the Python
derive assets; the deploy-from-main model stays.

**Crawler placement.** The datacenter-IP test ran 2026-10-26 (`experiments/xvfb-probe/RESULT.md`):
headed Chromium under Xvfb on a GitHub Actions runner (Azure, AS8075) cleared Cloudflare with no
challenge on all 8 requests. IP reputation is not the blocker, so the crawler is containerisable.
Dagster+ Serverless itself runs on AWS, which is untested, and its runs are capped at 4,500
step-minutes a day — ample for a ~7-minute peak crawl.

**Two routes to a fully cloud DAG, and why neither is in the plan:**
- **Xvfb + `headless=False` on Linux** — a real browser on a virtual display, so the
  headless-detection signals differ from true headless. Commonly used for exactly this and it may
  well work, but there is no evidence for it on *this* target: every measurement in the docs is
  macOS with real Chrome. Testing it is a side quest, not a Phase 2 commitment.
- **Browser-as-a-service** (Browserbase, Brightdata's scraping browser, Steel) — purpose-built
  hosted real browsers, which would let a cloud DAG do this directly. A new account, a cost, and
  an integration; viable, and the right answer if this had to be unattended on a server.

---

## 5. Layers

Four layers, mirroring the previous role's convention. `STAGING` holds dbt intermediate models and
is used sparingly.

### `LANDING` — everything, unfiltered
Append-only, one table per source entity, VARIANT payload plus `_fetched_at`, `_object_key`,
`_batch_id`, `_partition_date`. No filtering, no typing, no business logic.

```
lnd_tapology_events · lnd_tapology_event_details · lnd_tapology_bouts
lnd_tapology_event_fighters · lnd_tapology_bout_details
lnd_tapology_promotions · lnd_tapology_promotion_details
lnd_ufcstats_fights · lnd_ufcstats_fight_rounds · lnd_ufcstats_sig_position
lnd_ufcstats_sig_target · lnd_ufcstats_fighters · lnd_ufcstats_events
lnd_xref_bout_id_map · lnd_xref_fighter_id_map
lnd_mmadecisions_scorecards          ← the only new source
```

### Provenance already in the source
These are promoted to first-class LANDING audit columns rather than re-derived:

| field | use |
|---|---|
| `scraped_at` | exact per-record fetch timestamp — the true partition key |
| `event_date_index` + `date_precision` | dates already parsed (`full` / `assumed_year` / `from_page`) |
| `parse_ok` | per-record parse status |
| `bout_counts` vs `bout_counts_declared` + **`bout_counts_agree`** | a declared-vs-counted reconciliation that already exists |
| `index_promotion_id` vs `promotion.promotion_id` | the promotion-attribution audit |
| `order` / `order_derived` / `order_effective` + `order_source` | card position, with provenance |
| `region_source` | `derived_from_location` marks the 2,088 assigned regions |
| `source` | `sitemap+detail_backfill` marks backfilled rows |

### `CLEANSING` — hygiene only
Typing, dedup, trimming, `N/A` / `-` / empty → NULL, timestamp normalisation, natural-key
validation. **Still all sports, still all levels.** Hygiene is not scope.

**Tapology join keys** (measured 2026-10-25, every row):

| join | key | coverage |
|---|---|---|
| `events` → `event_details` | `event_slug` (exact, whole string) | 101,134 / 101,134 details rows |
| `event_details` → `bouts` | `event_slug` | 100% |
| `bouts` → `bout_details` | `bout_slug` | 58,176; the other 8,101 are cancelled title bouts (`bout-title-stakes.md`) |
| `bouts` → `event_fighters` | `bout_slug` | 1,640,865; the other 265,336 are fighters on cancelled/fizzled bouts |
| `event_details.promotion` → `promotions` | `promotion_id` | — |

Slug, not `event_id`: 528 early events use Tapology's legacy unprefixed URLs
(`ufc-93-franklin-vs-henderson`) and 5 of those parsed with a null `event_id` (Moosin, Yarennoka
NYE 2007, …). Their slugs still match exactly, so slug is the key and `event_id` the fallback.

**Scope filters for this build** — applied in `REFINED`, never in `LANDING`, so they stay
reversible: `event_details.status = 'Completed'` (drops 117 Upcoming, 32 In Progress,
3 Rumored Upcoming, 2 null) and the 610 `events` rows with no details page (IDs 131,371+, the
newest listings, whose pages were never fetched). The daily crawler fetches **completed events
only**, so one slug maps to one archived page.

### `REFINED` — purpose-fit models, and where scope lives
Two kinds of asset live here, and that is the point (§7):

**dbt (SQL) models**
- `dim_fighter`, `dim_promotion`, `dim_event`, `dim_weight_class`, `dim_belt`, `dim_judge`
- `fct_bout` (one row per bout, ~258k) · `fct_bout_participant` (one per fighter per bout, ~516k)
- `fct_ufc_round_stats` (one row per fighter per round, from the three UFC Stats files)
- `fct_judge_round_score` (one row per rater per round, ~33.6k judge-rounds from 2010) ← the ML grain

**Python assets — the existing derive layer, lifted into Dagster as-is**

| asset | script | output | runtime |
|---|---|---|---|
| `belts` | `derive/belts.py` | `belts.jsonl`, `bout_belts.jsonl` | seconds |
| `belt_reigns` | `derive/belt_lineage.py` | 50,543 reigns | ~8 s |
| `bout_title_stakes` | `derive/bout_title_stakes.py` | 59,787 (bout, belt) rows | ~10 s |
| `cancelled_title_bouts` | `derive/cancelled_bouts.py` | 8,101 title bouts, **from the HTML archive** | — |
| `bout_weigh_ins` | `derive/weigh_ins.py` | 22,012 weigh-ins | ~40 s |
| `fighter_population` | `derive/fighter_population.py` | gender + `in_population` | ~20 s |
| `promotion_strength` | `derive/promotion_strength.py` | 240,857 quarterly + 100,046 dated | ~2 min |
| `cross_sport_transfer` | `derive/cross_sport_transfer.py` | 19 ordered-pair `kappa` | ~40 s |

Whole chain ≈ 4 minutes, no network, documented dependency order
(`belts` → `belt_reigns` → `bout_title_stakes`).

### `PUBLISHED` — what the front end reads
- `pub_judge_lens` — per fight: actual scorecards, predicted scores per judge, counterfactual outcomes
- `pub_judge_profile` — per judge: striking- vs grappling-weighted, 10-8 rate, deviation from panel
- `pub_fighter_resume` — record plus the opposition-quality distribution behind it
- `pub_mma_strength` — MMA strength per promotion per year
- `pub_title_lineage` — belt → ordered reigns → lineal chain

### Terraform / dbt ownership boundary
| Owner | Objects |
|---|---|
| **Terraform** | Warehouse, database, **schemas**, roles, grants, storage integration, stage, file format, pipes |
| **dbt** | Tables and views *inside* those schemas — nothing else |

1. **dbt never creates a schema.** Set target schemas explicitly per folder; do not rely on
   `generate_schema_name`, or dbt invents schemas Terraform reports as drift forever.
2. **Terraform never manages a table.** Grants on dbt-built relations come from **future grants**
   (`ON FUTURE TABLES IN SCHEMA`), so a table dbt creates at 02:10 inherits privileges without
   Terraform knowing it exists.

Pin the `snowflakedb/snowflake` provider — it changed hands twice and the grant resources were
reworked on the way to v1, so most older examples no longer apply.

### Environments: DEV and PROD

Two databases with identical schemas, the convention from the previous role:

| | `FIGHTGRADE_PROD` | `FIGHTGRADE_DEV` |
|---|---|---|
| holds | what `main` builds | whatever you are working on |
| written by | `DBT_PROD_SVC` → `FIGHTGRADE_TRANSFORMER_PROD` only | you and `DBT_DEV_SVC` → `FIGHTGRADE_TRANSFORMER_DEV` |
| read by | `FIGHTGRADE_READER` (Streamlit, `PUBLISHED` only), and DEV (read-only) | — |
| dbt target | `--target prod` | default |

**Resync** is `dbt run-operation resync_dev_from_prod`: every PROD table is zero-copy cloned over
its DEV counterpart and DEV-only tables are dropped (`drop_dev_only: false` keeps them), so a reset costs seconds and no storage until either side diverges. DEV can
`SELECT` PROD (via future grants, which is all a clone needs) but holds no write privilege on it,
so a resync pointed the wrong way fails instead of damaging PROD. Views are not clonable; the next
DEV build recreates them. Scheduled as a Dagster job once Phase 3 exists; manual until then.

What makes PROD "what the repo has" is credentials, not discipline: humans hold only the DEV role.
The PROD key lives on the laptop for now and moves to Secret Manager / the orchestrator with
Phase 3, after which nothing local can write PROD.

---

## 6. The daily DAG (02:00) and the two backfills

```
LOCAL EDGE (launchd LaunchAgent, GUI session, headed Chrome)
   │
   ├─ tapology_discover      /fightcenter?schedule=results — newest-first, ~25 rows/page
   │                          (discovery half already built: driver/_parked/crawl_daily.WIP.py,
   │                           a real run found 8 new events and refreshed 50)
   ├─ tapology_event_pages   raw HTML → GCS BEFORE parsing
   ├─ ufcstats_crawl         headed Chrome only (JS browser check since 2026-10) — pipeline/ufcstats.py
   └─ mmadecisions_cards     static HTML, decisions only
          │
          ▼  GCS parsed/ zone
CLOUD
   ├─ copy_into_landing      → LANDING      (asset check: row-count reconciliation)
   ├─ dbt_build              CLEANSING → REFINED → PUBLISHED, tests at each gate
   ├─ derive_python_assets   the 8 scripts above, into REFINED
   └─ (weekly) retrain_judge_model   Snowpark → Model Registry → SQL function
```

### Backfilling the Tapology gap needs the SITEMAP, not `schedule=results`

`schedule=results` **caps at ~15 pages** (~375 events) — "recent results only, not an archive"
(`HANDOFF.md` §7). The 62-day gap is ~1,750 events, so it does not fit.

The documented completeness method does: `https://www.tapology.com/sitemap.xml` is a sitemap index
of 476 sub-sitemaps, of which **46 are events**. 46 requests enumerated 104,984 slugs last time;
diff against our 105,007 and crawl the difference. That is exactly how the original 17,410-event
blind spot was found.

Two dead ends, recorded so they are not retried: `schedule=results` (above), and **bare event ids
do not resolve** — `/fightcenter/events/97108` redirects to the homepage, the full slug is required.

### Three load paths, all real

| path | partitions | source | network |
|---|---|---|---|
| **Bulk historical** | 2 (`scraped_at` Aug 22–23) | the 8.8 GB local archive | **none** |
| **Tapology gap** | **62** (Aug 23 → Oct 24) | sitemap diff → **~1,515** all-sport events | ~40 min at 38/min |
| **UFC Stats gap** | ~45 events since 2025-12-13 | headed Chrome, `ufcstats_crawl` queue | ~2 h at 4 s, over a few daily runs |
| **Daily forward** | 1/day | `schedule=results` + UFC Stats + MMADecisions | **<20 s typical, ~7 min peak Saturday** |

The daily job lands **all sports**, not just MMA — LANDING is unfiltered by definition, and at 24
events/day there is no reason to narrow it. Scope still happens once, in REFINED (§3).

A daily schedule cannot be demonstrated by waiting. The 62 partitions exercise it 62 times
against genuinely missing events, and the demo shows a populated partition grid.

### Entity resolution — what is left
Only MMADecisions → `ufcstats_fight_id`. Blocking key `(event_date, sorted(fighter_surnames))`,
fall back to normalised names in a ±1-day window, unresolved pairs to an override seed — the same
pattern as `data/overrides/title_belts.jsonl`, which is fenced by three rules worth copying:
**an override can only attach to a row we already hold; every injected row names its source;
unmatched rows are skipped silently, never fabricated.** Match rate is published as a metric.

---

## 7. Why Dagster, not Airflow — the strongest version of the argument

Previous role used Airflow. Dagster wins here for a reason that comes out of the code, not a
feature list:

**Half of the REFINED layer is Python that cannot be expressed in SQL.** `promotion_strength.py`
alone computes ten signals, shrinks on two dimensions, z-scores against a per-sport field median,
and applies a per-sport logistic with fitted weights. `belt_lineage.py` walks a lineage with five
precedence rules. These are not dbt models and never will be.

With `dagster-dbt`, those eight Python assets and every dbt model sit in **one lineage graph**.
With Airflow you get tasks that happen to run dbt, and dbt's lineage lives separately — so nothing
shows you that `pub_fighter_resume` depends on `promotion_strength` which depends on `belts`.

Two more, both concrete here:
- **Daily partitions.** Backfilling 62 partitions of one asset is a first-class operation.
- **Asset checks** attach to the asset, which is where the data-quality gates belong (§9).

Interview framing: *"I shipped Airflow in production last co-op. I chose Dagster here because half
my transformation layer is Python, and Dagster is the only one of the two that puts Python and SQL
in a single lineage graph."*

---

## 8. The judge model

**Grain:** one row per `(ufcstats_fight_id, round, rater)`. A rater is a judge or the crowd.
Judge rows come from `cln_mmadecisions_judge_rounds`, lined up with UFC Stats through
`xref_decision_ufcstats_fight` (`ufcstats_fighter_id_1/_2` are MMADecisions' fighter order).
**Universe:** matched decisions from 2010 on, ~33.6k judge-rounds. Before 2010 the round stats
exist but the target does not: MMADecisions knows 837 of 4,554 pre-2010 judge-rounds; the rest
are "-" with only the judge's total.

### Target: the round score, as an ordered class

**Seven ordered classes, from fighter A's side:** 7-10 < 8-10 < 9-10 < 10-10 < 10-9 < 10-8 < 10-7.
One model gives both the round winner, P(A wins) = P(class > 10-10), and the margin. The binary
target of the first draft is just the model's round-winner probability.

| Score (after deductions are added back) | Judge-rounds | Share |
|---|---|---|
| 10-9 | 33,215 | 96.6% |
| 10-8 | 1,132 | 3.3% |
| 10-10 | 22 | 0.06% |
| 10-7 | 4 | 0.01% |

- **10-10 stays.** Two of the 22 decided a draw (3330, 6483). A model that cannot output 10-10
  cannot re-tally those cards. It costs one global cut-point (±τ) and no judge term.
- **10-7 stays its own class.** It gets a cut-point beyond 10-8 and will almost never be predicted;
  nothing is folded together.
- **10-8 is mostly a judge's call.** Only 132 of the 681 rounds with any 10-8 had it from all three
  judges. 10-8s come from control more than damage (median control gap 208 s; 17.8% have a
  knockdown). The rate moves with the rules: ~5% in 2016–21, 1.7–2.9% from 2022.

**Deductions are added back before the label.** MMADecisions' round scores include them: Long's
−2 in R3 shows as 9-8 or 10-7. The fan round scores include them too ("9-8 Garbrandt" at 83% is
Long 10-9). `stg_mmadecisions_deductions` holds them, with
`seeds/mmadecisions_deduction_overrides.csv` for pages that get them wrong. For the official card,
the deductions are applied after predicting the rounds; they are never the judge's opinion.

**What is not a target** (`tests/mmadecisions_round_scores_valid.sql` holds every points round to
"winner on 10, loser on 7-10" after the add-back):
- **Legend cards:** UFC 1–UFC Japan cards whose numbers are codes ("1 -" = favoured, "1-1" = draw).
  The extractor marks them `totals_kind = 'legend'` and nulls their rounds.
- **Quarantined decisions** (`seeds/mmadecisions_round_score_quarantine.csv`): 1018, where the
  cards put Swick's point in R1 and every source puts it in R3.
- **Rounds with no control time**, dropped from training (60 of 33,498 scored 2010+ rounds, all
  2010–12). UFC Stats' `--`
  means the fight was not tracked, never 0. It covers a whole fight or none of it (1,585 fights),
  and 2018 DWCS fights show it with 5 takedowns and 16 ground strikes. Zero control is `0:00`.
  The published output marks those rounds "control unknown".

**The order trap.** Fighter 1 is the decision winner, so in that order it's 10-9 at 75.6% against
9-10 at 20.8%. A model that knows which fighter is listed first learns "fighter 1 wins". The model
therefore gives the same answer whichever fighter is listed first: every feature is an A − B
difference, there is no intercept, the cut-points are symmetric, and training sees both
orientations. Swapping the fighters mirrors every probability exactly; it is a registration check.

### Raters

| Rater | Learned from | Count |
|---|---|---|
| **Judges** | Their hard round calls | 541; ~21 with enough data to name (300+ rounds, 2010–24) |
| **Crowd** | The fan round distribution, as a soft label (`cln_mmadecisions_fan_scores`) | 1 |
| **Average judge** | Nothing extra: all judge terms at zero | 1 |

- **The crowd is one more rater, not another target.** The population weights are fitted on judges
  only, so they stay the officials' standard. The crowd gets its own style terms, which its
  thousands of rounds pin down. Each crowd round is weighted n/(n + M_FAN), not by the raw count:
  3,000 fans are not 3,000 independent observations, and 2,800 of 4,312 decisions have fewer than
  50 fan scorecards. Fans are never a feature: they have seen the result.
- **The average judge is the yardstick.** It is weighted by rounds judged, which is how real
  panel seats are filled (22 judges hold 64% of the rounds). It is never trained as rows of its
  own, which would count the same judges twice. Unknown and new judges get it. "Crosby weights X
  1.4× the average judge" is literally his style term.
- **The panel is not a rater.** P(at least 2 of 3 give the round to A) comes from the three
  judges' predictions. The panel decides the fight; the average judge measures it.

### The model: shared terms, a style vector per rater

**Scoring terms, all shared.** Every term is a difference times something symmetric (unchanged when
the fighters swap), which keeps the order guarantee:
- **Main effects with diminishing returns** (splines or signed log): knockdowns, sig strikes
  landed and attempted, non-sig landed, head/body/leg, TDs landed and attempted, control, sub
  attempts, reversals. One strike decomposition at a time (head + body + leg = sig landed =
  distance + clinch + ground).
- **Gates and interactions:** Δcontrol × [any sub attempt in the round]; Δcontrol × ground strikes
  landed by both ("active vs lay-and-pray"); Δsig × [no knockdown], next to ΔKD; ΔTD × total
  control.
- **Context only on the cut-points:** era buckets for the 10-8 line (2017 criteria change). Round
  number, 5-round and title bout are candidates, not v1.

**Score for rater j:** the population weights plus B·z_j, applied to the terms. z_j is a small
style vector (rank r, 3 or 4 numbers); B is learned, the directions in which judges actually
differ. Each rater also gets its own 10-8 cut-point offset.
- **A judge can value anything, not "striking vs grappling".** One judge's vector can discount raw
  control and reward control-with-sub-attempts. Nothing is fixed in advance.
- **Small judges borrow from big ones.** About 30 terms × 541 judges is far too many for the
  2,428 split rounds where judges disagree (of 10,942 with three known judges). B is learned
  mostly from the busiest judges; a judge with 30 rounds gets a few numbers, not 30.
- **Shrinkage is the house style.** Each z_j and cut-point offset is pulled toward 0, the average
  judge, with pseudo-counts M_z and M_108. This is the same discipline as `M_INTAKE = 25`,
  `M_MATCH = 50`, `M_TITLE = 8` in `promotion_strength.py`.
- **The data decides who gets a profile.** A judge's own values show up mainly when the judge
  breaks from the other two. The 7 judges with 1,000+ rounds have a median of 106 lone dissents
  each; 300–999, 38; 100–299, 15; under 100, 3 or fewer. So detail exists for ~20 judges; for the
  rest the honest answer is "close to the average judge".
- **v2, if the data asks for it:** a second score for the 10-8 line, with its own small judge
  vector, for judges whose 10-8s follow damage rather than control (only 1,128 10-8 rows).
- **Benchmark:** LightGBM with judge as a category tests how much nonlinearity the shared terms
  miss. If it clearly wins, the next step is a small neural net that keeps the order guarantee,
  with the z_j as judge embeddings.

**Fitting:** PyTorch (available in Snowflake), seconds per fit; the low-rank fit is non-convex but
tiny. Inference is a few matrix products, exported to numpy, so the model is a numpy-only
`CustomModel` in the registry.

### Training, validation and testing

**Split by event, never by row.** One panel usually works a whole card, and all three judges share
each round's stats. Splitting rows would let the model see Lee's R2 while being graded on Crosby's
R2. Crowd rows follow their fight. Fold assignment is a hash of the event id, stored as a `split`
column on the training table, so new fights never reshuffle old ones.

| Period | Events | Fights | Judge-rounds | 10-8s | Role |
|---|---|---|---|---|---|
| 2010–2022 | 516 | 2,645 | 24,249 | 911 | Five-fold cross-validation, grouped by event: picks the hyperparameters |
| 2023–2024 | 85 | 530 | 5,076 | 146 | Forward check: train on 2010–22, score here; catches drift |
| 2025–2026 | 78 | 451 | 4,293 | 71 | Locked test, opened once |

**Hyperparameters, tuned in stages** so the selection does not overfit the 2,428 split rounds:

| Stage | Hyperparameter | Grid | Chosen on |
|---|---|---|---|
| A. Population (z = 0) | spline knots | linear, 3, 5 | judge log loss |
| | interaction set | none / gates / gates + TD×control | judge log loss |
| | era buckets on the cut-points | none / pre- & post-2017 / three | judge log loss |
| | L2 on the population weights | 3 values | judge log loss |
| B. Judge style | rank r | 0, 1, 2, 3, 4, 6 | log loss on the named judges' rows |
| | M_z | 10, 25, 50, 100, 200 | same |
| | M_108 | 10, 50, 200 | 10-8 log loss |
| C. Crowd | M_FAN | 10, 50, 200 | crowd soft-label log loss |

r = 0 is the average-judge model; stage B earns its place only by beating it. **Selection:**
mean held-out log loss over the five folds, then the one-standard-error rule: the simplest
configuration within one SE of the best wins.

**Measured on every run, on held-out predictions only:**
- **Overall:** log loss (selects), Brier, calibration error and a reliability curve for P(won
  round) and for P(10-8), accuracy.
- **Where judge styles should show:** log loss on the named judges' rows, on lone-dissent rounds,
  and on judges with few rounds.
- **10-8:** PR-AUC and log loss; accuracy at 3% means nothing.
- **Fight level:** P(the actual card), enumerating the rounds with the real deductions.
- **Baselines:** "more sig strikes wins" (77.1%, with 4.2% of rounds tied), "a knockdown wins"
  (90.1%), the average judge, LightGBM. Human ceiling: a judge sides with the other two 91.3% of
  the time when they agree, and the other two split on 14.8% of rounds.
- **The three test fights** (below), shown every run and never used to choose.

**Sequence:**
1. Cross-validation on 2010–22, stages A → B → C.
2. Forward check on 2023–24. If it is clearly worse than CV, revisit the era buckets before going on.
3. **Locked test, once:** refit on 2010–24, score 2025–26, report it as it comes out. No tuning
   after seeing it.
4. **Production fit** on everything with the same settings; registered as a new version.
5. **Named-judge claims:** bootstrap by event (~200 refits). A quirk reaches `pub_judge_profile`
   only if its interval excludes zero and it improved that judge's held-out log loss.

**Checks before a version becomes the default:**
- Swapping the fighters mirrors every probability exactly.
- The average judge matches the stage-A population model.
- Calibration error under a fixed bar.
- Forward-check log loss no worse than the current default plus a small tolerance.
- The three fights render, with deductions kept apart and the crowd row present.
- The training table's feature list holds no result, method or fan field.

**The named judges** (300+ scored rounds 2010–24): D'Amato, Cleary, Lee, Bell, Kamijo, Colón,
Weeks, McCarthy, Byrd, Cartlidge, Rosales, Crosby, Trowbridge, Hagen, Collett, Lethaby, Tirelli,
Hughes, Urso, Winter, Bertrand. Rosales, Crosby, Trowbridge, Hagen, Hughes, Urso and Bertrand have
no rounds in 2025–26, so the time test cannot check their styles; cross-validation only, and the
profile page says so. Judges first seen in the test period start as the average judge and are
reported apart.

### The three test fights

| Fight | What it tests |
|---|---|
| **Pimblett def. Gordon**, UFC 282, 2022-12-10 (decision 13677, `144cd3f62591a5fb`) | Volume against control, judges against the crowd. 29-28 ×3 Pimblett; 90% of 3,561 fans had Gordon. R2: all three judges went Pimblett on +4 strikes, against Gordon's takedown and 2:07 of control |
| **Garbrandt def. Xiao Long**, UFC 326, 2026-03-07 (15938, `ecd16d14a138d7fb`) | Deductions kept apart from round winners. 28-27 ×3 after Long's −2 in R3; all three had Long 29-28 before it. Long out-landed him every round. In the locked test |
| **O'Malley def. Yan**, UFC 280, 2022-10-22 (13556, `5c3c4bcc6c746ca0`) | A split panel. 29-28, 29-28, 28-29; 4,858 of 6,727 fans (72%) had Yan |

Pimblett–Gordon and O'Malley–Yan are shown from the fold where their event was held out, so no
model scores a fight it trained on.

### What it gives

- **Per round:** each rater's call as a probability; how surprising the actual call was, against
  the average judge and against that judge's own style.
- **Per fight:** P(win / split / draw) for the actual panel, an average panel and the crowd;
  **judge roulette** (re-score under panels drawn the way judges are really assigned: how much did
  the panel decide this?); named swaps. The counterfactual holds the round statistics fixed, swaps
  z_j, predicts each round, applies the real deductions and re-tallies.
- **Per judge:** response curves of what they reward, a style map, predictability, agreement with
  the crowd, 10-8 tendency by era.
- **Per fighter:** decision luck (actual decisions against an average panel on the same stats) and
  style fit. Beyond the original scope: first to cut.
- **Data quality:** a card the judge's own model finds near-impossible is a candidate transcription
  error, as 1018 was.

### Platform

| Need | Snowflake piece |
|---|---|
| Exploring | Snowflake Notebooks, or local Jupyter with a Snowpark session on DEV |
| Experiment tracking | **Snowflake Experiments** (`ExperimentTracking`, snowflake-ml-python 1.19+, no extra charge): one experiment per stage, one run per configuration; fold metrics, reliability plots and the test-fight table as artifacts |
| Training | A Dagster job `judge_lens_train`, on demand or weekly: reads the REFINED facts, fits, logs the run, registers a version, runs the checks above. The fit is seconds, so a warehouse or the Dagster process is enough; no compute pool |
| The model | **Snowflake Model Registry**, a `CustomModel`, in a new `ML` schema per environment. Terraform creates the schema (dbt never creates schemas) and grants the transformer roles `CREATE MODEL` and `CREATE EXPERIMENT` |
| Serving | `pub_judge_lens` and `pub_judge_profile` are dbt models that call the default version from SQL. The daily build only predicts |

The model is linear in its terms, so if the Registry slips, a coefficients table and plain SQL
are a free fallback. snowflake-ml-python 2.6.0 resolves on the project's Python 3.13.

**Limits to state before being asked:**
- **Correlational, not causal.** Judges do not score independently; panel anchoring exists, and
  judges see damage, octagon control and momentum the stats do not. "Robbery" means the scoring
  disagrees with the stats, not with the truth.
- **A style is a preference, not bias.** Nothing here is about who the fighters are.
- **Modern only.** Before 2010 there are almost no round scores to learn from.
- **Not a pre-fight predictor.** It needs the round stats, so the earliest it works is right after
  the fight, before the decision is read.

**ClearML: still cut, and experiment tracking is no longer the trade-off.** ClearML is a whole
system to stand up for one small model that appears nowhere in Relay's stack. Snowflake
Experiments gives run tracking inside the platform already in use, so the platform stays four
tools and the hyperparameter search is still logged and comparable.

### Do NOT build the Elo engine
`rating-model.md` specifies a per-sport rating vector with cross-sport propagation, and states
plainly that **"the present engine cannot express this."** It is a design contract, not working
code, and three of its terms (`w_outcome`, `w_recency`, age drift) are open.

**Résumé Strength v1 is pure SQL instead:** for each fighter, the distribution of opponent record
quality `q = 0.6·winrate + 0.4·experience` (Laplace-smoothed, the same `q` used throughout the
project), each bout weighted by `promotion_strength` **at that bout's date** — joining
`promotion_strength_events.jsonl` (100,046 exact-date rows), which the docs call "what a consumer
joins on". No rating engine, no new modelling, and it rests on a component already validated at
AUC 0.9968 against Tapology's Major-Org label.

---

## 9. Data quality — the tests, and the real incident each one remembers

Every test below is a documented failure from this project. A test that cannot name the bug it
prevents gets cut.

| # | Test | The incident |
|---|---|---|
| 1 | Page-promotion vs `index_promotion_id` disagreement = exactly 4 rows (all 4353→1) | Road to UFC cross-lists UFC cards; **name-similarity heuristics gave ~5,000 false positives** (20 promotions share "EFC") |
| 2 | `bout_counts_agree` rate within threshold | Karate Combat 62 declares 2 grappling bouts on a 12-bout card — **the card is authoritative, the production block is not** |
| 3 | No bout maps to a weight class outside its `valid_from`/`valid_to` | UFC "Lightweight" = 170 lb pre-2001; "Bantamweight" = 155 lb in 2001, 135 lb from 2011 |
| 4 | ONE belts resolve to era-correct divisions; heavyweight excluded | ONE's 2018 realignment split **every** men's belt in two and collided with the neighbour below |
| 5 | `dim_belt.sport` populated directly, never via a bout join | **669 belts spanned more than one sport** — Rodtang and Demetrious Johnson in one lineage |
| 6 | No cancelled bout reaches `fct_bout_participant`; the 88 known result-bearing exceptions are quarantined, not dropped | 3,263 cancelled events removed; **88 of 166 dropped bouts carried real W/L results** |
| 7 | Row-count + population-base reconciliation at every layer gate | The AgencyAnalytics CI pattern |
| 8 | `fct_bout_participant` has exactly 2 rows per `bout_sk` | Grain integrity |
| 9 | `fighter_slug` unique in `dim_fighter`; never keyed on the leading integer | **1,028 names belong to more than one fighter** — eight men are called "Pedro Henrique"; two "Craig Jones" |
| 10 | No doubled values (`"Philippines Philippines"`, `"170.0 lbs 170.0 lbs"`) | Tapology ships desktop **and** mobile copies; the `md:hidden` strip must run first or you get 24 bout blocks where there are 12 |
| 11 | `title_kind = 'title'` only in belt facts; `'tournament'` excluded | Without it a title filter covers 18.1% of bouts instead of 6.9% — 12,511 IBJJF/ADCC bracket rounds |
| 12 | `roles_source` gated: `lineage` rows flagged as reconstruction | **34.5% of title stakes are lineage-derived at 83.9% accuracy.** A third of the corpus is a guess |
| 13 | `belt_reigns` filtered on `gap_to_previous` | Lineage disagreement is a completeness measure: **13.5%** at a normal 90–365-day cadence, **93.8%** at 5y+ |
| 14 | Weigh-in vs contracted weight: ≥91% within 1 lb | An independent check on `weight_badge` from a separately scraped field |
| 15 | No weight under 45 lb survives into a mart | A "2 lb" title was a `5 x 2` **round length**; `63,5kg` was parsed as 5 kg by a comma-decimal regex |
| 16 | Re-parse drift guard: every pre-existing key byte-identical | A pass shipped `bout_url: null` on 46,893 rows; caught only because the file shrank |
| 17 | MMADecisions match rate published, alerted on regression | Entity resolution degrading silently |
| 18 | Judge scorecards: 3 judges per fight, rounds sum to a valid decision | Scorecard parse errors |

dbt schema + singular tests, plus Dagster **asset checks** on the cross-cutting invariants.

### Quote the population
`fighter-graph-and-gender.md`: four population counts are all legitimate and differ —
**467,263** (all) / 397,355 (filtered) / 368,099 (giant component) / **365,907** (canonical).
Every published metric must state which it used. This belongs in the dbt model descriptions.

---

## 10. Non-goals — state these out loud

- **No Kafka / streaming.** Fights land in discrete events a few times a week.
- **No Elo engine.** §8.
- **No ClearML.** §8.
- **No new Tapology stages.** Stage 4 (fighter pages) is documented as **not needed** — measured,
  not assumed: profile fields are rendered into every bout (100% all-or-nothing across 87,935
  fighters with 3+ bouts), and DOB is derivable from `age_at_fight` + event date, verified to the
  day against Khabib and Pereira.
- **No non-MMA sports in REFINED facts.** Sport mix survives as promotion context.
- **No SCD2.**
- **No style model.** `fighter_style.md` is locked at UFC men's only; side exhibit if asked.
- **No dashboard framework.** One Streamlit page.

---

## 11. Plan

| Phase | Days | Deliverable |
|---|---|---|
| **0 — Foundation** | Sat 24 – Mon 26 | Repo + `git`; consolidate the two data locations; Tapology + UFC Stats → Parquet; DuckDB + dbt running CLEANSING → REFINED locally. **Smoke-test the Tapology crawler on day one.** |
| **1 — Cloud** | Tue 27 – Wed 28 | GCP project, CLI, Terraform; `terraform apply` provisions the GCS bucket **and** Snowflake (warehouse, db, 4 schemas, roles, future grants, integration, stage); `gcloud storage rsync` the 8.8 GB archive; `COPY INTO` → LANDING; dbt retargeted to Snowflake. **Also: run the datacenter-IP probe** (§4.4) — 20 minutes, decides whether the crawler can be cloud-hosted |
| **2 — Sources** | Thu 29 – Fri 30 | MMADecisions scraper + match to `ufcstats_fight_id`; UFC Stats 7-month incremental; Tapology 62-day gap via sitemap diff |
| **3 — Orchestration** | Sat 31 – Sun Nov 1 | Dagster asset graph: `dagster-dbt` + the 8 Python derive assets in one lineage; daily partitions; 62-partition backfill; 18 checks green |
| **4 — ML** | Mon 2 | Judge model in Snowpark; Model Registry; SQL function; `pub_judge_lens` + `pub_judge_profile` |
| **5 — Serving** | Tue 3 | Streamlit: Judge Lens (lead), Résumé Strength (second) |
| **6 — Freeze** | Wed 4 | **No new code.** Architecture diagram, cost figures, rehearsal |
| **Deep dive** | **Thu 5** | |

Phase 0 lands before the Tuesday prelim deliberately. DuckDB-first means Phase 1 is a storage-layer
swap, not a rewrite.

**Cut order if it slips:** (1) counterfactual UI → static table; (2) `pub_title_lineage` and
`pub_mma_strength` → built but not surfaced; (3) MMADecisions → ship Tapology + UFC Stats with the
judge model documented as designed but unbuilt.

**Phase 0 smoke test, and it is small.** The crawler has not run since August. What must work:
headed Chrome launches, clears Cloudflare, and fetches ~10 event pages. That is the whole test.
The ExpressVPN CLI is **not** on the critical path any more (§4.4), so a broken VPN setup no
longer blocks the build. If headed Chrome itself will not clear the challenge, the 62-day gap
cannot be filled and the incremental story falls back to UFC Stats + MMADecisions, with Tapology
loaded from the archive only.

Operational traps still worth keeping from `HANDOFF.md`: never run two crawlers (they race on the
archive temp file — keep the single-instance lock); `--sleep 1.0` is the measured throughput
ceiling, do not go below it; guard against Tapology's "Temporarily Unavailable" page, which
returns a normal status code and silently poisoned 450 events before it was caught; and never
advance the worklist on a soft failure.

### Provider choice: GCP, not AWS

The AWS account was suspended hours after signup on 2026-10-24 and then closed. Recoverable via
a support case, but not on this timeline — and the architecture in §4 is about **object storage**,
not about Amazon, so nothing in it depends on the vendor.

| | **GCP / GCS** | Cloudflare R2 | AWS S3 |
|---|---|---|---|
| available now | card + verify (~15 min) | **instant, email only** | blocked |
| free allowance | **$300 / 90 days** | 10 GB forever | $100, inaccessible |
| Snowflake stage | **native GCS** | `s3compat://` (R2 endpoints enabled by default) | native |
| auto-ingest | **Snowpipe via Pub/Sub** | none — scheduled `COPY INTO` | Snowpipe via SQS |
| compute for the crawler container | **Cloud Run / GCE** (e2-micro always-free) | none | Fargate/EC2 |
| secrets | Secret Manager | n/a | Secrets Manager |
| egress | $0.12/GB | **free** | $0.09/GB |
| Canadian region | **Montréal + Toronto** | global, no choice | ca-central-1 |

**Decision: GCP.** It is the only option that keeps the design 1:1 with the original — native
external stage, auto-ingest, compute for the crawler probe, secret management, one platform — and
$300 against a ~$2 workload is not a constraint. Cloudflare R2 is the 15-minute insurance policy
if GCP signup also stalls; it covers everything the 12-day plan actually needs, and the only loss
is auto-ingest.

**What actually happened: Snowflake is on AWS `us-east-2` (Ohio), the bucket on GCS `us-central1`.**
The plan was to put both on GCP `us-central1` (Snowflake on GCP in North America offers only
`us-central1` and `us-east4`, and no Canadian region). The 2026-10-25 trial signup asked for no
cloud or region and placed the account on `AWS_US_EAST_2`. It was kept, because at this volume a
cross-cloud read is not worth a second signup: Snowflake on any cloud can use a GCS bucket as an
external stage, and the only cost is GCS internet egress at ~$0.12/GB — **~$0.03 for all 249 MB
of Parquet**, fractions of a cent per daily increment. The bucket stays in `us-central1` for the
always-free e2-micro (the crawler IP probe). In production, co-locate them: the egress line grows
with every reload, and same-region keeps the data on one provider's network.

Canadian residency is not available on this stack either way; if asked, name AWS `ca-central-1`
(with S3) as what a deployment with a residency requirement would use.

**GCS integration is simpler than S3, and it weakens one of my own arguments.** §5 claimed
Terraform earns its keep on the circular IAM bootstrap — the storage integration emits an IAM
user ARN and external ID that must then appear in the role's trust policy. **That is
AWS-specific.** On GCS, `CREATE STORAGE INTEGRATION` returns a Snowflake-managed service account
email and you grant it `storage.objectViewer` on the bucket. One direction, no external ID, no
circularity. So on GCP, Terraform's justification is the ordinary one — reproducibility, review,
and teardown — not a clever dependency resolution. Say that rather than reusing the S3 argument;
an interviewer who knows GCS will notice.

What changed in the build (done 2026-10-24): `terraform/aws_lake.tf` → `terraform/gcs_lake.tf`,
a `google_storage_bucket` with the same lifecycle rules (Nearline in place of Glacier IR; GCS's
default 7-day soft delete turned off because versioning already covers recovery), `s3://` →
`gs://` in the key layout, `aws s3 sync` → `gcloud storage rsync`, and the load becomes a
`COPY INTO` asset in Dagster rather than an auto-ingest pipe. Not a redesign.

### Operational notes, learned the hard way

- **Turn the VPN off before signing up for anything.** A fresh account with a Toronto billing
  address connecting from a foreign exit node is the most likely reason the AWS account was
  flagged. GCP runs similar heuristics.
- **Never create a second account to get around a suspension.** Duplicate detection is part of
  why the first was flagged, and a closed AWS account's email cannot be reused anyway. Reactivate
  through a support case — free on any plan, 90-day window.
- **Check the trial's cloud and region after signup** (`SELECT CURRENT_REGION();`). The 2026-10-25 signup chose them silently.
- **Snowflake trial timing barely matters.** 30 days / $400 from signup, and idle time burns no credits — only running warehouses do. Started 2026-10-25 it lasts to Nov 24, well past the Nov 5 deep dive.

**Cost:** Snowflake trial $400/30 days. Warehouse XSMALL with a 60-second auto-suspend, and a GCP
budget alert. Storage at this volume is cents against $300 of credit.

---

## 12. What revision 1 got wrong

Recorded because the deep dive may ask how the design changed.

1. **Fargate for the crawler.** Revision 1 assumed it would containerise; revision 2 assumed it
   never could. **Both were wrong, and it was testable** (§4.4): headed bundled Chromium under
   Xvfb in a Linux container clears the challenge, with `navigator.webdriver: true` visible. Real
   Chrome and macOS are both unnecessary; *headed-ness* is the load-bearing property. The one open
   variable is datacenter IP reputation, tested in Phase 1. Revision 2 also over-stated the
   operational burden: the VPN pool, `caffeinate` and the supervisor were built for a 26-hour
   104k-page crawl, and a peak daily run is ~268 pages in 7 minutes using half of one IP's budget.
2. **Cross-source entity resolution as the biggest risk.** `bout_id_map.csv` and
   `fighter_id_map.csv` already map Tapology ↔ UFC Stats at 100% coverage. Only MMADecisions is
   unmatched (§2).
3. **`schedule=results` for the 62-day backfill.** It caps at ~15 pages. The gap needs the sitemap
   diff (§6).

Also under-scoped: the derive layer is eight scripts, not a vague "metric port" (§5); the raw
archive and its replay path already exist and are proven (§4.1); and the promotion-strength model
is already validated against two external sources (§8).

---

## 13. Deep-dive questions to have rehearsed

1. Why an object store *and* Snowflake? (→ §4.1 — the bytes are the only thing that cannot be recomputed)
2. Can the scraper run in the cloud? (→ §4.4 — I tested it: headed Chromium under Xvfb clears; the open variable is datacenter IP reputation, not the browser)
3. Why Dagster when you have Airflow experience? (→ §7 — half of REFINED is Python)
4. What does Terraform own vs dbt, and who grants SELECT on a table dbt just created? (→ §5, future grants)
5. Why does the MMA filter live in REFINED rather than LANDING or CLEANSING? (→ §3, scope is a purpose, not hygiene)
6. Why two fact tables for bouts? (→ §5, event grain vs fighter grain)
7. Tapology edits a past event — how does the correction propagate? (→ archive replay, §4.1)
8. Is the pipeline idempotent? How do you backfill one partition? 62? (→ §6)
9. How do you match a fight across sources, and what is your match rate? (→ §6)
10. Is there enough data for a per-judge model? (→ §8: for ~20 judges; everyone else is shrunk to the average judge)
11. What are the limits of the counterfactual? (→ §8, panel anchoring; correlational)
12. How do you know the data is right? (→ §9, each test with its incident)
13. Which population is that number computed over? (→ §9, four legitimate counts)
14. Your schedule missed a night — what happens? (→ §4.4, the partition is the contract)
15. What breaks first at 10× the data?
16. What would you do differently with another month?
