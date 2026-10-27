"""mmadecisions_crawl: judges' decisions -> the lake -> LANDING.

    mmadecisions_fetch -> mmadecisions_stage_parquet -> mmadecisions_upload_to_lake
        -> mmadecisions_check_lake -> mmadecisions_load_landing -> mmadecisions_check_landing

Six lnd_mmadecisions_* tables (pipeline/mmadecisions.py), and only those, so it
never races the other crawlers' LANDING loads. Headed Chrome, like the other
crawlers; requests at least 2.5 s apart per IP, MMADecisions' terms.

All of it (~9,300 pages) runs as parallel shards, the tapology_crawl drill, one
per egress IP (a run that lands on an IP another run holds stops before its
first request; MMADecisions refuses 34.216.9.66, where a run stops cleanly):

    1. backfill_plan=true                   plan: every event on the 32 year pages
    2. backfill_shard=k, backfill_shards=N  for each k, in parallel: fetch and upload
    3. backfill_plan=true again             completeness: events_to_fetch should be 0
    4. replay=true                          parse every archived page, load LANDING once

replay=true re-parses the latest copy of every archived page from the bucket
with no requests to MMADecisions: a parser change is replayed, never re-crawled.
"""
from pathlib import Path

from dagster import Config, Failure, OpExecutionContext, in_process_executor, job, op

import pipeline.mmadecisions as mmad
from pipeline.lake import Lake, environment, new_run_id, zone

from .dbt_cli import run_dbt
from .main_checkout import dbt_project_for
from .tapology import crawler_log_to


class MmaDecisionsCrawlConfig(Config):
    limit: int = 0                             # events to archive; 0 = no cap
    max_requests: int = mmad.DAILY_BUDGET      # requests this run may spend, within today's budget for the IP
    daily_budget: int = mmad.DAILY_BUDGET      # the per-IP daily cap; a backfill may raise it
    interval: float = mmad.MIN_INTERVAL        # seconds between request starts; mmad.FLOOR_INTERVAL is the floor
    max_minutes: float = mmad.MAX_FETCH_MINUTES
    backfill_plan: bool = False                # write the all-events plan, fetch no events
    backfill_shard: int = -1                   # with backfill_shards: fetch this shard of the plan
    backfill_shards: int = 0
    replay: bool = False                       # re-parse the archive and load LANDING; no requests


@op
def mmadecisions_fetch(context: OpExecutionContext, config: MmaDecisionsCrawlConfig) -> dict:
    if config.interval < mmad.FLOOR_INTERVAL:
        raise Failure(f"interval below {mmad.FLOOR_INTERVAL} s is faster than MMADecisions agreed to")
    backfill = config.backfill_plan or bool(config.backfill_shards)
    if config.backfill_shards and not 0 <= config.backfill_shard < config.backfill_shards:
        raise Failure("backfill_shard must be in 0 .. backfill_shards-1")
    env, run_id = environment(), new_run_id()
    context.log.info(f"run {run_id}, env {env}, zone '{zone(env)}'")
    shard = config.backfill_shard if config.backfill_shards else None
    with crawler_log_to(context, mmad):
        lake = Lake(env)
        if config.replay:
            result = mmad.run(env, run_id, lake, None, config.interval, do_replay=True)
        else:
            with mmad.ip_budget(lake, config.max_requests, config.max_minutes, config.daily_budget) as budget:
                if backfill and budget.limit == 0:
                    raise Failure("no budget on this egress IP (spent today, or another run holds it): "
                                  "launch this shard again")
                result = mmad.run(env, run_id, lake, budget, config.interval, config.limit, config.backfill_plan,
                                  shard, config.backfill_shards)
    if result["stopped"]:
        context.log.warning(result["stopped"])
    context.add_output_metadata({"run_id": run_id, "env": env, **result["report"]})
    return {"env": env, "run_id": run_id, "run_dir": str(mmad.RUNS / run_id), "result": result, "shard": shard,
            "backfill": backfill}


@op
def mmadecisions_stage_parquet(context: OpExecutionContext, fetched: dict) -> dict:
    run_dir = Path(fetched["run_dir"])
    for sub in ("tables", "parsed"):                 # staging is rebuilt whole
        mmad.shutil.rmtree(run_dir / sub, ignore_errors=True)
    staged = mmad.stage(fetched["result"]["rows"], run_dir, fetched["run_id"])
    context.add_output_metadata({f"parquet_rows_{t}": n for t, (n, _) in staged.items()} or {"parquet_rows": 0})
    return {**fetched, "staged": staged}


@op
def mmadecisions_upload_to_lake(context: OpExecutionContext, bundle: dict) -> dict:
    lake = Lake(bundle["env"])
    pages, files = mmad.upload(lake, bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"])
    mmad.save_state(lake, bundle["env"], bundle["result"], bundle["shard"], bundle["run_id"])
    context.log.info(f"uploaded {pages} raw pages and {files} Parquet files to zone '{zone(bundle['env'])}'")
    context.add_output_metadata({"raw_pages": pages, "parquet_files": files})
    return bundle


@op
def mmadecisions_check_lake(context: OpExecutionContext, bundle: dict) -> dict:
    checks = mmad.verify(bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"],
                         Lake(bundle["env"]))
    for w in checks["warns"][:200]:
        context.log.warning(w)
    context.add_output_metadata({"fails": len(checks["fails"]), "warnings": len(checks["warns"])})
    if checks["fails"]:
        raise Failure(f"{len(checks['fails'])} lake checks failed; nothing was loaded",
                      metadata={"fails": "\n".join(checks["fails"][:50])})
    return bundle


@op
def mmadecisions_load_landing(context: OpExecutionContext, bundle: dict) -> dict:
    if bundle.get("backfill"):
        context.log.info("backfill: LANDING is loaded once, by a replay=true run after every shard")
        return bundle
    if not bundle["staged"]:
        context.log.info("nothing new to load")
        return bundle
    run_dbt(context, dbt_project_for(bundle["env"]), "build", "--select", mmad.DBT_SELECT, "--target", bundle["env"])
    return bundle


@op
def mmadecisions_check_landing(context: OpExecutionContext, bundle: dict) -> None:
    """LANDING holds exactly this run's Parquet rows, per table."""
    if bundle.get("backfill") or not bundle["staged"]:
        context.log.info("nothing was loaded, nothing to reconcile")
        return
    counts = mmad.landing_counts(bundle["run_id"], bundle["staged"], bundle["env"])
    context.add_output_metadata({f"{t}": f"{c['landing']} of {c['parquet']}" for t, c in counts.items()})
    off = {t: c for t, c in counts.items() if c["landing"] != c["parquet"]}
    if off:
        raise Failure(f"LANDING does not match the Parquet for {sorted(off)}", metadata={"counts": str(off)})
    context.log.info(f"LANDING reconciled: {counts}")


@job(executor_def=in_process_executor, tags={"fightgrade/checkout": "main"})
def mmadecisions_crawl():
    mmadecisions_check_landing(mmadecisions_load_landing(mmadecisions_check_lake(mmadecisions_upload_to_lake(
        mmadecisions_stage_parquet(mmadecisions_fetch())))))
