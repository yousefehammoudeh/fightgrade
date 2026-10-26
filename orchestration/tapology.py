"""tapology_crawl: newly completed Tapology events -> the lake -> LANDING.

The same steps as `python -m pipeline.tapology`, one op each, so every stage
shows in the UI and a failed check stops the run before anything half-written
is loaded:

    tapology_fetch -> stage_parquet -> upload_to_lake -> check_lake
                   -> load_landing -> check_landing

It writes to the environment's zone (pipeline/lake.py): DEV by default, PROD
only on the Dagster+ production deployment or with FIGHTGRADE_ENV=prod.

Each run spends at most pipeline.tapology.DAILY_BUDGET requests a day from its
IP. What it cannot reach is queued in the lake and fetched by the next run.

A large backfill runs as parallel shards instead (one per egress IP; a run that
lands on an IP another run holds stops before its first request):

    1. backfill_until=YYYY-MM-DD           plan: the sitemap gap as one fixed list
    2. backfill_shard=k, backfill_shards=N  for each k, in parallel: fetch and upload
    3. dbt_prod_build with select=landing  one LANDING load for every shard

Shards skip their own LANDING load, because concurrent incremental loads of the
same stage would each insert the others' new files. A shard cut short is rerun
as is: it skips what is archived by now.

Tapology needs headed Chrome. On the Mac it uses the real display; on Dagster+
Serverless the image (Dockerfile) carries Chrome and Xvfb, and the crawler
starts a virtual display for itself. `from_run` re-processes a run's local
archive, so it only works where that run happened.
"""
from contextlib import contextmanager
from pathlib import Path

from dagster import Config, Failure, OpExecutionContext, in_process_executor, job, op

import pipeline.tapology as tap
from pipeline.lake import Lake, environment, new_run_id, zone

from .dbt_cli import run_dbt
from .main_checkout import IN_DAGSTER_PLUS, dbt_project_for


class TapologyCrawlConfig(Config):
    limit: int = 0                          # completed events (or bout pages) to keep; 0 = no cap
    max_requests: int = tap.DAILY_BUDGET    # requests this run may spend, within today's budget for the IP
    daily_budget: int = tap.DAILY_BUDGET    # the per-IP daily cap; a backfill may raise it
    sleep: float = tap.DEFAULT_SLEEP        # seconds between requests; 2.0 is the floor
    from_run: str = ""                      # re-process an archived run: no requests to Tapology
    title_bouts_since: str = ""             # YYYY-MM-DD: fetch missing title-bout pages instead
    max_minutes: float = tap.MAX_FETCH_MINUTES   # stop fetching after this; the default ends by 02:30
    backfill_until: str = ""                # YYYY-MM-DD: write the backfill plan, fetch no events
    backfill_shard: int = -1                # with backfill_shards: fetch this shard of the plan
    backfill_shards: int = 0


@contextmanager
def crawler_log_to(context: OpExecutionContext):
    """The crawler prints progress; send it to the Dagster run log instead."""
    original = tap.log
    tap.log = context.log.info
    try:
        yield
    finally:
        tap.log = original


@op
def tapology_fetch(context: OpExecutionContext, config: TapologyCrawlConfig) -> dict:
    if IN_DAGSTER_PLUS and config.from_run:
        raise Failure("from_run re-processes a local run folder; a Dagster+ run starts with none")
    if config.sleep < 2.0:
        raise Failure("sleep below 2.0 s draws Cloudflare challenges from one IP")

    backfill = bool(config.backfill_until or config.backfill_shards)
    if config.backfill_shards and not 0 <= config.backfill_shard < config.backfill_shards:
        raise Failure("backfill_shard must be in 0 .. backfill_shards-1")

    env = environment()
    run_id = config.from_run or new_run_id()
    run_dir = tap.RUNS / run_id
    context.log.info(f"run {run_id}, env {env}, zone '{zone(env)}'")
    with crawler_log_to(context):
        lake = Lake(env)
        known = tap.known_events(lake, env)
        context.log.info(f"{len(known):,} events already archived")
        if config.from_run:
            result = tap.rebuild(run_dir, env)
            known -= {e["event_slug"] for e in result["rows"]["event_details"]}
        else:
            with tap.ip_budget(lake, config.max_requests, config.max_minutes, config.daily_budget) as budget:
                if backfill and budget.limit == 0:
                    raise Failure("no budget on this egress IP (spent today, or another run holds it): "
                                  "launch this shard again")
                if config.backfill_until:
                    result = tap.plan_backfill(lake, env, config.backfill_until, known, config.sleep, budget)
                elif config.backfill_shards:
                    result = tap.backfill_shard(lake, env, run_dir, config.sleep, budget, known,
                                                config.backfill_shard, config.backfill_shards)
                elif config.title_bouts_since:
                    result = tap.backfill_title_bouts(config.title_bouts_since, config.limit, config.sleep,
                                                      budget, env, run_dir)
                else:
                    result = tap.daily_crawl(lake, env, run_dir, config.limit, config.sleep, budget, known)
    if result["stopped"]:
        context.log.warning(result["stopped"])
    context.add_output_metadata({"run_id": run_id, "env": env, **result["report"],
                                 **{f"rows_{t}": len(r) for t, r in result["rows"].items()}})
    if result.get("state"):
        context.log.info(f"{len(result['state']['queue'])} events queued for the next run")
    shard = config.backfill_shard if config.backfill_shards else None
    return {"env": env, "run_id": run_id, "run_dir": str(run_dir), "result": result, "known": known,
            "backfill": backfill, "shard": shard}


@op
def stage_parquet(context: OpExecutionContext, fetched: dict) -> dict:
    run_dir = Path(fetched["run_dir"])
    for sub in ("tables", "parsed"):                 # staging is rebuilt whole
        tap.shutil.rmtree(run_dir / sub, ignore_errors=True)
    staged = tap.stage(fetched["result"]["rows"], run_dir, fetched["run_id"])
    context.add_output_metadata({f"parquet_rows_{t}": n for t, (n, _) in staged.items()} or {"parquet_rows": 0})
    return {**fetched, "staged": staged}


@op
def upload_to_lake(context: OpExecutionContext, bundle: dict) -> dict:
    lake = Lake(bundle["env"])
    pages, files = tap.upload(lake, bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"])
    tap.save_queue(lake, bundle["env"], bundle["result"])
    if bundle.get("shard") is not None:     # what this shard hands back to the daily queue
        lake.write_json(tap.backfill_key(bundle["env"], f"shard-{bundle['shard']}"),
                        {"run_id": bundle["run_id"], "leftover": bundle["result"]["leftover"],
                         "abandoned": bundle["result"]["abandoned"]})
    context.log.info(f"uploaded {pages} raw pages and {files} Parquet files to zone '{zone(bundle['env'])}'")
    context.add_output_metadata({"raw_pages": pages, "parquet_files": files})
    return bundle


@op
def check_lake(context: OpExecutionContext, bundle: dict) -> dict:
    """Tests 4-6: raw archive, parse quality, Parquet round trip and bucket sizes."""
    checks = tap.verify(bundle["result"], bundle["staged"], Path(bundle["run_dir"]), bundle["env"],
                        Lake(bundle["env"]), bundle["known"])
    for w in checks["warns"]:
        context.log.warning(w)
    context.add_output_metadata({"fails": len(checks["fails"]), "warnings": len(checks["warns"])})
    if checks["fails"]:
        raise Failure(f"{len(checks['fails'])} lake checks failed; nothing was loaded",
                      metadata={"fails": "\n".join(checks["fails"][:50])})
    return bundle


@op
def load_landing(context: OpExecutionContext, bundle: dict) -> dict:
    if bundle.get("backfill"):
        context.log.info("backfill: LANDING is loaded once, after every shard (dbt_prod_build select=landing)")
        return bundle
    if not bundle["staged"]:
        context.log.info("nothing new to load")
        return bundle
    run_dbt(context, dbt_project_for(bundle["env"]), "build", "--select", "landing", "--target", bundle["env"])
    return bundle


@op
def check_landing(context: OpExecutionContext, bundle: dict) -> None:
    """Test 7: LANDING holds exactly this run's Parquet rows, per table."""
    if bundle.get("backfill"):
        context.log.info("backfill: reconciled after the single LANDING load")
        return
    if not bundle["staged"]:
        context.log.info("nothing was loaded, nothing to reconcile")
        return
    counts = tap.landing_counts(bundle["run_id"], bundle["staged"], bundle["env"])
    context.add_output_metadata({f"{t}": f"{c['landing']} of {c['parquet']}" for t, c in counts.items()})
    off = {t: c for t, c in counts.items() if c["landing"] != c["parquet"]}
    if off:
        raise Failure(f"LANDING does not match the Parquet for {sorted(off)}", metadata={"counts": str(off)})
    context.log.info(f"LANDING reconciled: {counts}")


@job(executor_def=in_process_executor, tags={"fightgrade/checkout": "main"})
def tapology_crawl():
    check_landing(load_landing(check_lake(upload_to_lake(stage_parquet(tapology_fetch())))))
