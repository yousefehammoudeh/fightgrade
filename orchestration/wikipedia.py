"""wikipedia_crawl: English Wikipedia's UFC event articles -> the lake -> LANDING.

    wikipedia_fetch -> wikipedia_stage_parquet -> wikipedia_upload_to_lake -> wikipedia_check_lake
        -> wikipedia_load_landing -> wikipedia_check_landing

One table, lnd_wikipedia_bout_changes (pipeline/wikipedia_events.py), and only
that, so it never races the other crawlers' LANDING loads. The MediaWiki API,
one request at a time, maxlag honoured, at least 2 s between requests, a user
agent with a contact, one run at a time.

The weekly run (Monday 07:00, once the weekend's card has been written up)
reads the list and every event dated within 60 days of today: ~20 requests.
backfill=true reads every event not archived yet, ~810 requests, under 40
minutes; one cut short is run again.

replay=true re-parses the latest archived copy of every page from the bucket,
with no requests to Wikipedia: a parser change is replayed, never re-crawled.
PROD's history is filled that way from a copy of DEV's archive:

    gcloud storage cp -r gs://fightgrade-lake/dev/raw-json/wikipedia gs://fightgrade-lake/raw-json/
    wikipedia_crawl with replay=true, on the production deployment
"""
from pathlib import Path

from dagster import Config, Failure, OpExecutionContext, in_process_executor, job, op

import pipeline.wikipedia_events as wiki
from pipeline.lake import Lake, environment, new_run_id, zone

from .dbt_cli import run_dbt
from .main_checkout import dbt_project_for
from .tapology import crawler_log_to, fail_if_nothing_fetched


class WikipediaCrawlConfig(Config):
    backfill: bool = False                  # every event not archived yet, not just those near today
    window_days: int = wiki.WINDOW_DAYS     # the weekly run reads events within this many days of today
    interval: float = wiki.MIN_INTERVAL     # seconds between request starts; 2 is the floor
    max_requests: int = 0                   # 0: wiki.WEEKLY_BUDGET, or wiki.BACKFILL_BUDGET for a backfill
    max_minutes: float = wiki.MAX_FETCH_MINUTES
    replay: bool = False                    # re-parse the archive and load LANDING; no requests


@op
def wikipedia_fetch(context: OpExecutionContext, config: WikipediaCrawlConfig) -> dict:
    if config.interval < wiki.MIN_INTERVAL:
        raise Failure(f"interval below {wiki.MIN_INTERVAL} s breaks the Wikipedia crawl's pace")
    env, run_id = environment(), new_run_id()
    context.log.info(f"run {run_id}, env {env}, zone '{zone(env)}'")
    budget = config.max_requests or (wiki.BACKFILL_BUDGET if config.backfill else wiki.WEEKLY_BUDGET)
    with crawler_log_to(context, wiki):
        if config.replay:
            result = wiki.replay(Lake(env), env, run_id)
        else:
            result = wiki.run(env, run_id, Lake(env), config.backfill, config.interval, budget, config.max_minutes,
                              config.window_days)
    if result["stopped"]:
        context.log.warning(result["stopped"])
    context.add_output_metadata({"run_id": run_id, "env": env, **result["report"]})
    fail_if_nothing_fetched(result)
    return {"env": env, "run_id": run_id, "run_dir": result["run_dir"], "result": result}


@op
def wikipedia_stage_parquet(context: OpExecutionContext, fetched: dict) -> dict:
    run_dir = Path(fetched["run_dir"])
    for sub in ("tables", "parsed"):                 # staging is rebuilt whole
        wiki.shutil.rmtree(run_dir / sub, ignore_errors=True)
    staged = wiki.stage(fetched["result"]["rows"], run_dir, fetched["run_id"])
    context.add_output_metadata({f"parquet_rows_{t}": n for t, (n, _) in staged.items()} or {"parquet_rows": 0})
    return {**fetched, "staged": staged}


@op
def wikipedia_upload_to_lake(context: OpExecutionContext, bundle: dict) -> dict:
    pages, files = wiki.upload(Lake(bundle["env"]), bundle["result"], bundle["staged"], Path(bundle["run_dir"]),
                               bundle["env"])
    context.log.info(f"uploaded {pages} pages and {files} Parquet files to zone '{zone(bundle['env'])}'")
    context.add_output_metadata({"raw_pages": pages, "parquet_files": files})
    return bundle


@op
def wikipedia_check_lake(context: OpExecutionContext, bundle: dict) -> dict:
    checks = wiki.verify(bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"],
                         Lake(bundle["env"]))
    for w in checks["warns"][:200]:
        context.log.warning(w)
    context.add_output_metadata({"fails": len(checks["fails"]), "warnings": len(checks["warns"])})
    if checks["fails"]:
        raise Failure(f"{len(checks['fails'])} lake checks failed; nothing was loaded",
                      metadata={"fails": "\n".join(checks["fails"][:50])})
    return bundle


@op
def wikipedia_load_landing(context: OpExecutionContext, bundle: dict) -> dict:
    if not bundle["staged"]:
        context.log.info("nothing new to load")
        return bundle
    run_dbt(context, dbt_project_for(bundle["env"]), "build", "--select", wiki.DBT_SELECT, "--target", bundle["env"])
    return bundle


@op
def wikipedia_check_landing(context: OpExecutionContext, bundle: dict) -> None:
    """LANDING holds exactly this run's Parquet rows."""
    if not bundle["staged"]:
        context.log.info("nothing was loaded, nothing to reconcile")
        return
    want, got = bundle["staged"][wiki.TABLE][0], wiki.landing_count(bundle["run_id"], bundle["env"])
    context.add_output_metadata({wiki.TABLE: f"{got} of {want}"})
    if got != want:
        raise Failure(f"LANDING lnd_{wiki.TABLE} holds {got} of this run's {want} rows")
    context.log.info(f"LANDING reconciled: {got} rows")


@job(executor_def=in_process_executor, tags={"fightgrade/checkout": "main"})
def wikipedia_crawl():
    wikipedia_check_landing(wikipedia_load_landing(wikipedia_check_lake(wikipedia_upload_to_lake(
        wikipedia_stage_parquet(wikipedia_fetch())))))
