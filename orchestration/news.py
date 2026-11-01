"""news_crawl: MMA Fighting's and MMA Mania's monthly sitemaps -> the lake -> LANDING.
news_classify: the new candidate headlines -> events, after each news_crawl's build.

    news_fetch -> news_stage_parquet -> news_upload_to_lake -> news_check_lake
        -> news_load_landing -> news_check_landing

One table, lnd_news_urls (pipeline/news_sitemaps.py), and only that, so it never
races the other crawlers' LANDING loads. Plain HTTP with an honest user agent,
sitemaps only, at least 6 s between requests on a site, one run per site.

The daily run (05:00, after the 02:00 Tapology run and its build) reads each
site's current and previous month: 4 requests. backfill=true reads every month
not archived yet, ~515 requests, under an hour; one cut short is run again.

replay=true re-parses the latest archived copy of every month from the bucket,
with no requests to the sites: a parser change is replayed, never re-crawled.
PROD's history is filled that way from a copy of DEV's archive:

    gcloud storage cp -r gs://fightgrade-lake/dev/raw-xml/news gs://fightgrade-lake/raw-xml/
    news_crawl with replay=true, on the production deployment

A run whose every site stops before archiving anything (a refusal, a limit,
another run holding the lock) fails, and the crawl_retry sensor launches it
again, usually from another egress IP.

news_classify (pipeline/news_classify.py, pipeline/news_articles.py) runs after
the PROD build that follows each news_crawl, by the classify_after_news_crawl
sensor, so the day's headlines are in stg_news_headlines:

    news_classify_headlines -> news_read_articles -> news_classify_articles

The model runs in the run's own container: Ollama is in the image (CPU only), the
weights (~2.5 GB) are pulled from Ollama's registry at the start and checked
against news_classify.MODEL_DIGEST. Pulling them per run, not baking them into
the image, keeps every other job's start fast: Fargate caches no image layers.
A day brings ~6 candidates (p95 11) and ~2 flagged articles (p95 5). Measured on
Serverless (2026-11-01): the pull ~15 s, then ~25 s a headline and ~50 s an
article, and ~2 min for each step's first request (the long fixed prompt);
~10-15 min a day in all. The articles are
read under the same rules as the sitemaps (honest user agent, >= 6 s a site, the
site's lock). A site that refuses is left for the next day: a page is fetched
until it is archived. The PROD build after it (build_after_crawl) rebuilds
int_news_events.
"""
from pathlib import Path

from dagster import Config, Failure, OpExecutionContext, in_process_executor, job, op

import pipeline.news_articles as articles
import pipeline.news_classify as classifier
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
    replay: bool = False                    # re-parse the archive and load LANDING; no requests


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
        if config.replay:
            result = news.replay(Lake(env), env, run_id)
        else:
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


class NewsClassifyConfig(Config):
    limit: int = 300                        # candidate headlines per run, newest first (~10 s each)
    article_limit: int = 100                # flagged articles to read per run
    workers: int = 1                        # CPU Ollama serves one request at a time
    interval: float = news.MIN_INTERVAL     # seconds between article requests on a site; 6 is the floor


def load_with(context: OpExecutionContext, select: str):
    """A finish() load: this code location's dbt for the target's LANDING table."""
    def load(env: str) -> bool:
        run_dbt(context, dbt_project_for(env), "build", "--select", select, "--target", env)
        return True
    return load


def classify_step(context: OpExecutionContext, config: NewsClassifyConfig, of_articles: bool) -> dict:
    env, run_id = environment(), new_run_id()
    model = classifier.MODEL
    context.log.info(f"run {run_id}, env {env}, {model} "
                     f"{classifier.ARTICLE_PROMPT_VERSION if of_articles else classifier.PROMPT_VERSION}")
    with crawler_log_to(context, classifier):
        with classifier.model_server(model, classifier.RUNS / f"ollama-{run_id}.log"):
            result = classifier.run(env, run_id, model, config.article_limit if of_articles else config.limit,
                                    config.workers, of_articles)
        report = classifier.finish(env, run_id, model, result, of_articles,
                                   load=load_with(context, classifier.DBT_SELECT))
    context.add_output_metadata({k: v for k, v in report.items() if isinstance(v, (int, str))})
    if report["fails"]:
        raise Failure(f"{len(report['fails'])} checks failed", metadata={"fails": "\n".join(report["fails"][:50])})
    return report


@op
def news_classify_headlines(context: OpExecutionContext, config: NewsClassifyConfig) -> dict:
    """Every candidate headline with no answer yet from the model's slug prompt."""
    return classify_step(context, config, of_articles=False)


@op
def news_read_articles(context: OpExecutionContext, config: NewsClassifyConfig, headlines: dict) -> dict:
    """The pages behind the flagged headlines not archived yet -> the lake -> LANDING."""
    if config.interval < news.MIN_INTERVAL:
        raise Failure(f"interval below {news.MIN_INTERVAL} s breaks the news crawl's rule")
    env, run_id = environment(), new_run_id()
    lake = Lake(env)
    with crawler_log_to(context, articles):
        todo = articles.todo_flagged(env, lake, config.article_limit)
        result = articles.run(env, run_id, lake, todo, config.interval, config.article_limit,
                              articles.MAX_FETCH_MINUTES)
        report = articles.finish(env, run_id, result, lake, load=load_with(context, articles.DBT_SELECT))
    for w in report["warns"][:200]:
        context.log.warning(w)
    context.add_output_metadata({k: v for k, v in report.items() if isinstance(v, (int, str))})
    if report["fails"]:
        raise Failure(f"{len(report['fails'])} checks failed", metadata={"fails": "\n".join(report["fails"][:50])})
    return report


@op
def news_classify_articles(context: OpExecutionContext, config: NewsClassifyConfig, read: dict) -> None:
    """Every article read with no answer yet from the model's article prompt."""
    classify_step(context, config, of_articles=True)


@job(executor_def=in_process_executor, tags={"fightgrade/checkout": "main"})
def news_classify():
    news_classify_articles(news_read_articles(news_classify_headlines()))
