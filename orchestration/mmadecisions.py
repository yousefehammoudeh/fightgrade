"""mmadecisions_crawl: judges' decisions -> raw HTML in the lake.

    mmadecisions_fetch -> mmadecisions_upload_to_lake -> mmadecisions_check_lake

Raw HTML only for now (pipeline/mmadecisions.py): the decision-page tables are
extracted from the archive once their format is agreed, and LANDING is loaded
then. Headed Chrome, like the other crawlers; requests at least 2.5 s apart per
IP, MMADecisions' terms.

All of it (~9,300 pages) runs as parallel shards, the tapology_crawl drill, one
per egress IP (a run that lands on an IP another run holds stops before its
first request):

    1. backfill_plan=true                   plan: every event on the 32 year pages
    2. backfill_shard=k, backfill_shards=N  for each k, in parallel: fetch and upload
    3. backfill_plan=true again             completeness: events_to_fetch should be 0

A shard cut short is rerun as is: it skips what is archived by now. A shard
needs ~3,100 requests, so launch it with max_requests, daily_budget and
max_minutes raised.
"""
from pathlib import Path

from dagster import Config, Failure, OpExecutionContext, in_process_executor, job, op

import pipeline.mmadecisions as mmad
from pipeline.lake import Lake, environment, new_run_id, zone

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
        with mmad.ip_budget(lake, config.max_requests, config.max_minutes, config.daily_budget) as budget:
            if backfill and budget.limit == 0:
                raise Failure("no budget on this egress IP (spent today, or another run holds it): "
                              "launch this shard again")
            result = mmad.run(env, run_id, lake, budget, config.interval, config.limit, config.backfill_plan,
                              shard, config.backfill_shards)
    if result["stopped"]:
        context.log.warning(result["stopped"])
    context.add_output_metadata({"run_id": run_id, "env": env, **result["report"]})
    return {"env": env, "run_id": run_id, "result": result, "shard": shard}


@op
def mmadecisions_upload_to_lake(context: OpExecutionContext, bundle: dict) -> dict:
    lake = Lake(bundle["env"])
    n = mmad.upload(lake, bundle["result"])
    mmad.save_state(lake, bundle["env"], bundle["result"], bundle["shard"], bundle["run_id"])
    context.log.info(f"uploaded {n} raw pages to zone '{zone(bundle['env'])}'")
    context.add_output_metadata({"raw_pages": n})
    return bundle


@op
def mmadecisions_check_lake(context: OpExecutionContext, bundle: dict) -> None:
    checks = mmad.verify(bundle["result"], Lake(bundle["env"]))
    for w in checks["warns"]:
        context.log.warning(w)
    context.add_output_metadata({"fails": len(checks["fails"]), "warnings": len(checks["warns"])})
    if checks["fails"]:
        raise Failure(f"{len(checks['fails'])} lake checks failed", metadata={"fails": "\n".join(checks["fails"][:50])})


@job(executor_def=in_process_executor, tags={"fightgrade/checkout": "main"})
def mmadecisions_crawl():
    mmadecisions_check_lake(mmadecisions_upload_to_lake(mmadecisions_fetch()))
