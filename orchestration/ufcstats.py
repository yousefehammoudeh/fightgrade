"""ufcstats_crawl: newly completed UFC Stats events -> the lake -> LANDING.

The same steps as `python -m pipeline.ufcstats`, one op each, like tapology_crawl:

    ufcstats_fetch -> stage_parquet -> upload_to_lake -> check_lake
                   -> load_landing -> check_landing

It writes to the environment's zone (pipeline/lake.py) and loads only the
lnd_ufcstats_* tables, so it never races tapology_crawl's LANDING load.

UFC Stats is reached only through headed Chrome: plain HTTP gets a browser
check. On Dagster+ Serverless the image (Dockerfile) carries Chrome and Xvfb,
exactly as for Tapology.

All of UFC Stats (~16,800 pages) runs as parallel shards, the tapology_crawl
drill, one per egress IP (a run that lands on an IP another run holds fails
before its first request, and the crawl_retry sensor launches it again):

    1. backfill_plan=true                   plan: every listed event and every fighter, ~28 requests
    2. backfill_shard=k, backfill_shards=N  for each k, in parallel: fetch and upload
    3. dbt_prod_build with select=path:models/landing/ufcstats
                                            one LANDING load for every shard
    4. backfill_plan=true again             completeness: its events_to_fetch should be ~0;
                                            if not, repeat 2-3 (shards skip what is archived)

The completed-events list leaves ~475 events out (DWCS, PRIDE, UFC 1, ...), so
each shard fetches its fighters first and also claims the unplanned events their
pages link to, split by event id so no two shards fetch the same one.

Shards skip their own LANDING load, because concurrent incremental loads of the
same stage would each insert the others' new files. A shard cut short is rerun
as is: it skips what is archived by now. A shard needs ~5,600 requests, so
launch it with max_requests, daily_budget and max_minutes raised.
"""
from pathlib import Path

from dagster import Config, Failure, OpExecutionContext, in_process_executor, job, op

import pipeline.ufcstats as ufc
from pipeline.lake import Lake, environment, new_run_id, zone

from .dbt_cli import run_dbt
from .main_checkout import IN_DAGSTER_PLUS, dbt_project_for
from .tapology import RETRY_ON_ANOTHER_IP, crawler_log_to, fail_if_nothing_fetched


class UfcStatsCrawlConfig(Config):
    limit: int = 0                          # completed events to keep; 0 = no cap
    max_requests: int = ufc.DAILY_BUDGET    # requests this run may spend
    daily_budget: int = ufc.DAILY_BUDGET    # a backfill's per-IP daily cap; a daily run has none
    sleep: float = ufc.DEFAULT_SLEEP        # seconds between requests; ufc.MIN_SLEEP is the floor
    max_minutes: float = ufc.MAX_FETCH_MINUTES
    since: str = ufc.EXPORT_LAST_EVENT      # only events dated after this
    from_run: str = ""                      # re-process an archived run: no requests to UFC Stats
    backfill_plan: bool = False             # write the all-of-UFC-Stats plan, fetch no events
    backfill_shard: int = -1                # with backfill_shards: fetch this shard of the plan
    backfill_shards: int = 0


@op
def ufcstats_fetch(context: OpExecutionContext, config: UfcStatsCrawlConfig) -> dict:
    if IN_DAGSTER_PLUS and config.from_run:
        raise Failure("from_run re-processes a local run folder; a Dagster+ run starts with none")
    if config.sleep < ufc.MIN_SLEEP:
        raise Failure(f"sleep below {ufc.MIN_SLEEP} s is faster than anything measured")
    backfill = config.backfill_plan or bool(config.backfill_shards)
    if config.backfill_shards and not 0 <= config.backfill_shard < config.backfill_shards:
        raise Failure("backfill_shard must be in 0 .. backfill_shards-1")
    env = environment()
    run_id = config.from_run or new_run_id()
    run_dir = ufc.RUNS / run_id
    context.log.info(f"run {run_id}, env {env}, zone '{zone(env)}'")
    with crawler_log_to(context, ufc):
        lake = Lake(env)
        known = ufc.known_events(lake, env)
        context.log.info(f"{len(known):,} UFC Stats events already archived")
        if config.from_run:
            result = ufc.rebuild(run_dir, env)
            known -= {e["ufcstats_event_id"] for e in result["rows"]["ufcstats_events"]}
        else:
            daily = config.daily_budget if backfill else None
            with ufc.ip_budget(lake, config.max_requests, config.max_minutes, daily) as budget:
                if backfill and budget.limit == 0:
                    raise Failure(f"no budget on this egress IP (spent today, or another run holds it): "
                                  f"{RETRY_ON_ANOTHER_IP}")
                if config.backfill_plan:
                    result = ufc.plan_backfill(lake, env, config.sleep, budget, known)
                elif config.backfill_shards:
                    result = ufc.backfill_shard(lake, env, run_dir, config.sleep, budget, known,
                                                config.backfill_shard, config.backfill_shards)
                else:
                    state = lake.read_json(ufc.queue_key(env), {})
                    result = ufc.crawl(config.limit, config.sleep, budget, env, run_dir, known, state, config.since)
    if result["stopped"]:
        context.log.warning(result["stopped"])
    context.add_output_metadata({"run_id": run_id, "env": env, **result["report"],
                                 **{f"rows_{t}": len(r) for t, r in result["rows"].items()}})
    fail_if_nothing_fetched(result)
    shard = config.backfill_shard if config.backfill_shards else None
    return {"env": env, "run_id": run_id, "run_dir": str(run_dir), "result": result, "known": known,
            "backfill": backfill, "shard": shard}


@op
def ufcstats_stage_parquet(context: OpExecutionContext, fetched: dict) -> dict:
    run_dir = Path(fetched["run_dir"])
    for sub in ("tables", "parsed"):                 # staging is rebuilt whole
        ufc.shutil.rmtree(run_dir / sub, ignore_errors=True)
    staged = ufc.stage(fetched["result"]["rows"], run_dir, fetched["run_id"])
    context.add_output_metadata({f"parquet_rows_{t}": n for t, (n, _) in staged.items()} or {"parquet_rows": 0})
    return {**fetched, "staged": staged}


@op
def ufcstats_upload_to_lake(context: OpExecutionContext, bundle: dict) -> dict:
    lake = Lake(bundle["env"])
    pages, files = ufc.upload(lake, bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"])
    ufc.save_queue(lake, bundle["env"], bundle["result"])
    if bundle.get("shard") is not None:     # what this shard hands back for a rerun
        ufc.save_shard_leftover(lake, bundle["env"], bundle["shard"], bundle["run_id"], bundle["result"])
    context.log.info(f"uploaded {pages} raw pages and {files} Parquet files to zone '{zone(bundle['env'])}'")
    context.add_output_metadata({"raw_pages": pages, "parquet_files": files})
    return bundle


@op
def ufcstats_check_lake(context: OpExecutionContext, bundle: dict) -> dict:
    checks = ufc.verify(bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"],
                        Lake(bundle["env"]), bundle["known"])
    for w in checks["warns"]:
        context.log.warning(w)
    context.add_output_metadata({"fails": len(checks["fails"]), "warnings": len(checks["warns"])})
    if checks["fails"]:
        raise Failure(f"{len(checks['fails'])} lake checks failed; nothing was loaded",
                      metadata={"fails": "\n".join(checks["fails"][:50])})
    return bundle


@op
def ufcstats_load_landing(context: OpExecutionContext, bundle: dict) -> dict:
    if bundle.get("backfill"):
        context.log.info(f"backfill: LANDING is loaded once, after every shard "
                         f"(dbt_prod_build select={ufc.DBT_SELECT})")
        return bundle
    if not bundle["staged"]:
        context.log.info("nothing new to load")
        return bundle
    run_dbt(context, dbt_project_for(bundle["env"]), "build", "--select", ufc.DBT_SELECT, "--target", bundle["env"])
    return bundle


@op
def ufcstats_check_landing(context: OpExecutionContext, bundle: dict) -> None:
    """LANDING holds exactly this run's Parquet rows, per table."""
    if bundle.get("backfill"):
        context.log.info("backfill: reconciled after the single LANDING load")
        return
    if not bundle["staged"]:
        context.log.info("nothing was loaded, nothing to reconcile")
        return
    counts = ufc.landing_counts(bundle["run_id"], bundle["staged"], bundle["env"])
    context.add_output_metadata({f"{t}": f"{c['landing']} of {c['parquet']}" for t, c in counts.items()})
    off = {t: c for t, c in counts.items() if c["landing"] != c["parquet"]}
    if off:
        raise Failure(f"LANDING does not match the Parquet for {sorted(off)}", metadata={"counts": str(off)})
    context.log.info(f"LANDING reconciled: {counts}")


@job(executor_def=in_process_executor, tags={"fightgrade/checkout": "main"})
def ufcstats_crawl():
    ufcstats_check_landing(ufcstats_load_landing(ufcstats_check_lake(ufcstats_upload_to_lake(
        ufcstats_stage_parquet(ufcstats_fetch())))))
