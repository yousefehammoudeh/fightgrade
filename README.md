# FightGrade

An MMA data platform built on a complete Tapology scrape: **820,880 bouts** across
**101,744 events** and **7,007 promotions**, plus round-level UFC Stats and judge scorecards.

Two products off one pipeline:

1. **Judge Lens** (UFC) — how would *this specific judge* have scored this fight, and would a
   different judge have changed the result?
2. **Résumé Strength** (all pro MMA) — fight records lie; quantify *who a fighter actually beat*,
   era-accurately.

> **Start here: [`DESIGN.md`](DESIGN.md)** — scope, architecture, data model, the 18 data-quality
> tests and the build plan. It is the spec; this file is just the map.

---

## Layout

| path | what |
|---|---|
| `pipeline/` | the crawlers (Tapology, UFC Stats, MMADecisions, Wikipedia, news) → raw HTML in GCS → Parquet → LANDING |
| `pipeline/parsers/` | one HTML parser per source, pure functions of a page, so a fix replays over the archive |
| `pipeline/tapology_archive/` | the stage-3 Tapology archive crawl and its replays (`reparse*.py`) |
| `dbt/` | LANDING → CLEANSING → REFINED → PUBLISHED on Snowflake, with the data tests |
| `orchestration/` | Dagster+ definitions: crawls, the PROD build, DEV resync, `judge_lens_train` |
| `ml/judge_lens/` | the judge model: training, the Registry, panels, publishing (`DESIGN.md` §8) |
| `apps/judge_lens/` | the Judge Lens page, Streamlit in Snowflake |
| `terraform/` | GCS, Snowflake (DEV and PROD), roles and grants |
| `derive/` | the 8-script derive layer (belts, lineage, title stakes, promotion strength); offline, not yet in Dagster |
| `scripts/` | archive staging, and the promotion-strength tuning and validation |
| `experiments/` | spikes with written-up results |
| `DESIGN.md` | **the spec** — read before changing anything |
| `docs/` | design docs: the scrape method, field census, data fixes, the strength and rating models, belts and lineage. **Authoritative on how the data behaves.** |
| `docs/history/` | crawl-era handoffs and operational traps (cited by `pipeline/tapology.py` and `DESIGN.md`) |
| `data/` | the datasets + the 8.8 GB raw HTML archive — **gitignored** |

Legacy tools (the style analysis, the title-fight viewer, stage-2 crawlers, one-off fixes) stay on
disk but out of git; `.gitignore` lists them.

## ⚠️ This repo sits on ~15 GB of data

`data/`, `logs/`, `outputs/`, `graph/` and `samples/` are gitignored. The raw HTML archive alone
is **170,678 files**. Check `git status` before any `git add .`.

## Current state

| | |
|---|---|
| Sources | Tapology daily, UFC Stats and MMADecisions weekly, on Dagster+; each crawl rebuilds PROD |
| Warehouse | Snowflake DEV and PROD through PUBLISHED, built by dbt from `main` |
| Judge Lens | trained, registered (`ML.JUDGE_LENS`) and retrained weekly; published tables, views and the Streamlit page |
| Résumé Strength | not built yet (`DESIGN.md` §11) |
| Derive layer | 8 scripts, ~4 min end to end, no network; not yet in Dagster |

## Running things

```bash
cd dbt && ../.venv/bin/dbt build --target dev             # DEV; PROD is what main builds
orchestration/run_dagster.sh                               # Dagster UI on :3000
.venv/bin/python -m ml.judge_lens.deploy                   # the training procedure, DEV
.venv/bin/streamlit run apps/judge_lens/streamlit_app.py   # Judge Lens, against DEV
```

**Never re-crawl to fix a parser bug.** Fix the extractor and replay the archive: in Dagster,
`mmadecisions_crawl` with `replay: true`; for the stage-3 Tapology corpus:

```bash
python3 -m pipeline.tapology_archive.reparse --commit      # events,  ~3.5 h, zero network
python3 -m pipeline.tapology_archive.reparse3b --commit    # bouts,   ~140 s
```

Crawling the corpus took 26 hours. Replaying it takes 3.5. That asymmetry is the whole reason the
archive exists — see `DESIGN.md` §4.1.
