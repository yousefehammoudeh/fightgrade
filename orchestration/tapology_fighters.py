"""tapology_fighter_crawl: fighter pages -> every bout on their records -> the lake.

The same steps as `python -m pipeline.tapology_fighters`, one op each:

    fighter_fetch -> fighter_stage -> fighter_upload -> fighter_check_lake

Run as parallel shards of the plan (one per egress IP), the way the event backfill
runs: launch shard k of N for each k. The plan is written once, from a laptop, with
`python -m pipeline.tapology_fighters --plan`: it comes from the local record census.
A shard that lands on an IP another run holds, or one with no budget left today, fails
before its first request and crawl_retry launches it again. A shard cut short is
launched again as is: it skips what is archived by now.

Shards do not load LANDING themselves (two incremental loads at once insert each
other's files twice). build_after_crawl runs the PROD build once no crawl is in
progress, and its LANDING models load every shard's files in one go.
"""
from pathlib import Path

from dagster import Config, Failure, OpExecutionContext, in_process_executor, job, op

import pipeline.tapology as tap
import pipeline.tapology_fighters as tf
from pipeline.lake import Lake, environment, new_run_id, zone

from .tapology import RETRY_ON_ANOTHER_IP, crawler_log_to, fail_if_nothing_fetched


class FighterCrawlConfig(Config):
    shard: int = 0
    shards: int = 1
    slugs: str = ""                         # comma-separated: fetch these instead of a shard
    limit: int = 0                          # fighter pages to keep; 0 = no cap
    max_requests: int = tf.DAILY_BUDGET     # requests this run may spend
    daily_budget: int = tf.DAILY_BUDGET     # per-IP cap shared by every shard today
    sleep: float = tap.DEFAULT_SLEEP
    max_minutes: float = 240.0              # 1,500 pages at 4 s is ~100 min


@op
def fighter_fetch(context: OpExecutionContext, config: FighterCrawlConfig) -> dict:
    if config.sleep < 2.0:
        raise Failure("sleep below 2.0 s draws Cloudflare challenges from one IP")
    if not 0 <= config.shard < config.shards:
        raise Failure("shard must be in 0 .. shards-1")
    env = environment()
    run_id = new_run_id()
    run_dir = tap.RUNS / run_id
    context.log.info(f"run {run_id}, env {env}, zone '{zone(env)}'")
    with crawler_log_to(context):                  # pipeline.tapology_fighters logs through tap.log
        lake = Lake(env)
        if config.slugs:
            todo = [s.strip() for s in config.slugs.split(",") if s.strip()]
        else:
            mine = tf.shard_slugs(lake, env, config.shard, config.shards)
            done = tf.archived(lake, env)
            todo = [s for s in mine if s not in done]
            context.log.info(f"shard {config.shard}/{config.shards}: {len(mine):,} fighters, "
                             f"{len(todo):,} not archived yet")
        if not todo:
            context.log.info("nothing left to fetch in this shard")
            return {"env": env, "run_id": run_id, "run_dir": str(run_dir), "result": None}
        with tap.ip_budget(lake, config.max_requests, config.max_minutes, config.daily_budget) as budget:
            if budget.limit == 0:
                raise Failure(f"no budget on this egress IP (spent today, or another run holds it): "
                              f"{RETRY_ON_ANOTHER_IP}")
            result = tf.crawl(todo, config.limit, config.sleep, budget, env, run_dir)
    if result["stopped"]:
        context.log.warning(result["stopped"])
    context.add_output_metadata({"run_id": run_id, "env": env, "planned": len(todo), **result["report"]})
    fail_if_nothing_fetched(result)
    return {"env": env, "run_id": run_id, "run_dir": str(run_dir), "result": result}


@op
def fighter_stage(context: OpExecutionContext, fetched: dict) -> dict:
    if fetched["result"] is None:
        return {**fetched, "staged": {}}
    run_dir = Path(fetched["run_dir"])
    for sub in ("tables", "parsed"):                 # staging is rebuilt whole
        tap.shutil.rmtree(run_dir / sub, ignore_errors=True)
    staged = tap.stage(fetched["result"]["rows"], run_dir, fetched["run_id"])
    context.add_output_metadata({f"parquet_rows_{t}": n for t, (n, _) in staged.items()} or {"parquet_rows": 0})
    return {**fetched, "staged": staged}


@op
def fighter_upload(context: OpExecutionContext, bundle: dict) -> dict:
    if bundle["result"] is None:
        return bundle
    pages, files = tap.upload(Lake(bundle["env"]), bundle["result"], bundle["staged"], Path(bundle["run_dir"]),
                              bundle["env"])
    context.log.info(f"uploaded {pages} raw pages and {files} Parquet files to zone '{zone(bundle['env'])}'")
    context.add_output_metadata({"raw_pages": pages, "parquet_files": files})
    return bundle


@op
def fighter_check_lake(context: OpExecutionContext, bundle: dict) -> None:
    """Archive, parse quality, Parquet round trip and bucket sizes. LANDING is loaded by
    the PROD build after the shards, not here."""
    if bundle["result"] is None:
        return
    checks = tf.verify(bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"], Lake(bundle["env"]))
    for w in checks["warns"]:
        context.log.warning(w)
    context.add_output_metadata({"fails": len(checks["fails"]), "warnings": len(checks["warns"])})
    if checks["fails"]:
        raise Failure(f"{len(checks['fails'])} lake checks failed", metadata={"fails": "\n".join(checks["fails"][:50])})


@job(executor_def=in_process_executor, tags={"fightgrade/checkout": "main"})
def tapology_fighter_crawl():
    fighter_check_lake(fighter_upload(fighter_stage(fighter_fetch())))
