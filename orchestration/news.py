"""news_crawl: MMA Fighting's and MMA Mania's monthly sitemaps -> the lake -> LANDING.

    news_fetch -> news_stage_parquet -> news_upload_to_lake -> news_check_lake
        -> news_load_landing -> news_check_landing

One table, lnd_news_urls (pipeline/news_sitemaps.py), and only that, so it never
races the other crawlers' LANDING loads. Plain HTTP with an honest user agent,
sitemaps only, at least 6 s between requests on a site, one run per site.

The daily run (05:00, after the 02:00 Tapology run and its build) reads each
site's current and previous month: 4 requests. backfill=true reads every month
not archived yet, ~515 requests, under an hour; one cut short is run again.

A run whose every site stops before archiving anything (a refusal, a limit,
another run holding the lock) fails, and the crawl_retry sensor launches it
again, usually from another egress IP.
"""
from pathlib import Path

from dagster import Config, Failure, OpExecutionContext, in_process_executor, job, op

import pipeline.news_sitemaps as news
from pipeline.lake import Lake, environment, new_run_id, zone

from .dbt_cli import run_dbt
from .main_checkout import dbt_project_for
from .tapology import crawler_log_to, fail_if_nothing_fetched


class NewsCrawlConfig(Config):
    sites: list[str] = sorted(news.SITES)
    backfill: bool = False                  # every month not archived yet, not just the last two
    interval: float = news.MIN_INTERVAL     # seconds between request starts on a site; 6 is the floor
    max_requests: int = 0                   # 0: news.DAILY_BUDGET, or news.BACKFILL_BUDGET for a backfill
    max_minutes: float = news.MAX_FETCH_MINUTES


@op
def news_fetch(context: OpExecutionContext, config: NewsCrawlConfig) -> dict:
    if config.interval < news.MIN_INTERVAL:
        raise Failure(f"interval below {news.MIN_INTERVAL} s breaks the news crawl's rule")
    unknown = set(config.sites) - set(news.SITES)
    if unknown:
        raise Failure(f"unknown sites {sorted(unknown)}; known: {sorted(news.SITES)}")
    env, run_id = environment(), new_run_id()
    context.log.info(f"run {run_id}, env {env}, zone '{zone(env)}'")
    budget = config.max_requests or (news.BACKFILL_BUDGET if config.backfill else news.DAILY_BUDGET)
    with crawler_log_to(context, news):
        result = news.run(env, run_id, Lake(env), config.sites, config.backfill, config.interval, budget,
                          config.max_minutes)
    if result["stopped"]:
        context.log.warning(result["stopped"])
    context.add_output_metadata({"run_id": run_id, "env": env, **result["report"]})
    fail_if_nothing_fetched(result)
    return {"env": env, "run_id": run_id, "run_dir": result["run_dir"], "result": result}


@op
def news_stage_parquet(context: OpExecutionContext, fetched: dict) -> dict:
    run_dir = Path(fetched["run_dir"])
    for sub in ("tables", "parsed"):                 # staging is rebuilt whole
        news.shutil.rmtree(run_dir / sub, ignore_errors=True)
    staged = news.stage(fetched["result"]["rows"], run_dir, fetched["run_id"])
    context.add_output_metadata({f"parquet_rows_{t}": n for t, (n, _) in staged.items()} or {"parquet_rows": 0})
    return {**fetched, "staged": staged}


@op
def news_upload_to_lake(context: OpExecutionContext, bundle: dict) -> dict:
    pages, files = news.upload(Lake(bundle["env"]), bundle["result"], bundle["staged"], Path(bundle["run_dir"]),
                               bundle["env"])
    context.log.info(f"uploaded {pages} sitemaps and {files} Parquet files to zone '{zone(bundle['env'])}'")
    context.add_output_metadata({"raw_sitemaps": pages, "parquet_files": files})
    return bundle


@op
def news_check_lake(context: OpExecutionContext, bundle: dict) -> dict:
    checks = news.verify(bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"],
                         Lake(bundle["env"]))
    for w in checks["warns"][:200]:
        context.log.warning(w)
    context.add_output_metadata({"fails": len(checks["fails"]), "warnings": len(checks["warns"])})
    if checks["fails"]:
        raise Failure(f"{len(checks['fails'])} lake checks failed; nothing was loaded",
                      metadata={"fails": "\n".join(checks["fails"][:50])})
    return bundle


@op
def news_load_landing(context: OpExecutionContext, bundle: dict) -> dict:
    if not bundle["staged"]:
        context.log.info("nothing new to load")
        return bundle
    run_dbt(context, dbt_project_for(bundle["env"]), "build", "--select", news.DBT_SELECT, "--target", bundle["env"])
    return bundle


@op
def news_check_landing(context: OpExecutionContext, bundle: dict) -> None:
    """LANDING holds exactly this run's Parquet rows."""
    if not bundle["staged"]:
        context.log.info("nothing was loaded, nothing to reconcile")
        return
    want, got = bundle["staged"][news.TABLE][0], news.landing_count(bundle["run_id"], bundle["env"])
    context.add_output_metadata({news.TABLE: f"{got} of {want}"})
    if got != want:
        raise Failure(f"LANDING lnd_{news.TABLE} holds {got} of this run's {want} rows")
    context.log.info(f"LANDING reconciled: {got} rows")


@job(executor_def=in_process_executor, tags={"fightgrade/checkout": "main"})
def news_crawl():
    news_check_landing(news_load_landing(news_check_lake(news_upload_to_lake(news_stage_parquet(news_fetch())))))
