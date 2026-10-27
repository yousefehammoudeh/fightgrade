"""Dagster definitions for FightGrade.

Deployed to Dagster+ Serverless from main (see .github/workflows/), or run
locally for development with:

    orchestration/run_dagster.sh      UI on http://localhost:3000

Jobs, all built from main (main_checkout.py) except DEV runs of tapology_crawl:

  dbt_prod_build   PROD = main. Daily at 03:00, or launched by hand from the UI
                   for a rebuild now — optionally narrowed with `select`.
  dbt_resync_dev   DEV = PROD, after every successful PROD build (the 03:00
                   one, or one launched by hand), by the resync_dev sensor.
                   Zero-copy clones, so it takes seconds; it drops DEV-only
                   tables, so unmerged DEV work lasts until the next build.
  tapology_crawl   newly completed Tapology events -> lake -> LANDING
                   (orchestration/tapology.py). Daily at 02:00, Tapology's
                   quiet hour: its audience is ~41% US, then the UK and central
                   Europe, so 2 a.m. Eastern falls after US midnight and before
                   Europe's morning. Its fetch stops by 02:30, so LANDING is
                   loaded before the 03:00 build publishes it.
  ufcstats_crawl   newly completed UFC Stats events -> lake -> LANDING
                   (orchestration/ufcstats.py). Weekly, Sunday 06:00: UFC Stats
                   only follows the UFC, whose cards are on Saturdays, and a
                   US card ends ~01:00, so by 06:00 its results are posted.
                   That is after the 03:00 build, so a card reaches the models
                   with Monday's build. A different site from Tapology, its
                   own lock and its own LANDING tables. A UFC card is ~45
                   requests, ~3 min.
  mmadecisions_crawl  judges' decisions -> lake -> LANDING
                   (orchestration/mmadecisions.py). Weekly, Sunday 06:00, for
                   the same reasons; its own six lnd_mmadecisions_* tables.
                   It re-reads events up to 14 days old, so each card is read
                   the morning after and again a week later.

A crawl that stops before archiving anything fails, and the crawl_retry sensor
launches it again with the same config: Serverless gives each run one of 3
egress IPs, and MMADecisions refuses one of them.

Schedules and the sensors run only on the Dagster+ production deployment.
Locally they start stopped, so a laptop never races the cloud for the same 03:00
build, and a branch deployment builds DEV instead of PROD.
"""
from pathlib import Path

from dagster import (
    Config,
    DagsterRunStatus,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    Definitions,
    OpExecutionContext,
    RunRequest,
    RunFailureSensorContext,
    RunStatusSensorContext,
    ScheduleDefinition,
    SkipReason,
    in_process_executor,
    job,
    op,
    run_failure_sensor,
    run_status_sensor,
)

from .dbt_cli import run_dbt
from .main_checkout import IN_DAGSTER_PLUS, IS_BRANCH_DEPLOYMENT, main_dbt_project
from .tapology import RETRY_ON_ANOTHER_IP, tapology_crawl
from .mmadecisions import mmadecisions_crawl
from .ufcstats import ufcstats_crawl

TIMEZONE = "America/Toronto"
DAILY_0200, SUNDAY_0600 = "0 2 * * *", "0 6 * * 0"
ON_PROD = IN_DAGSTER_PLUS and not IS_BRANCH_DEPLOYMENT
SCHEDULE_STATUS = DefaultScheduleStatus.RUNNING if ON_PROD else DefaultScheduleStatus.STOPPED
SENSOR_STATUS = DefaultSensorStatus.RUNNING if ON_PROD else DefaultSensorStatus.STOPPED

# Locally both jobs reset the same checkout; dagster.yaml queues them so they
# never overlap.
CHECKOUT_TAG = {"fightgrade/checkout": "main"}


@op
def checkout_main(context: OpExecutionContext) -> str:
    project, sha = main_dbt_project()
    context.log.info(f"building from {sha} ({project})")
    context.add_output_metadata({"commit": sha, "dbt_project": str(project)})
    return str(project)


class ProdBuildConfig(Config):
    # Empty builds everything on main. A dbt selector ("landing",
    # "lnd_tapology_bouts+") narrows a manual run.
    select: str = ""
    # Rebuild incremental tables from every file in the stage instead of
    # appending new ones — for a deliberate reload, e.g. after the lake's files
    # were re-staged. Never set by the schedule.
    full_refresh: bool = False


@op
def dbt_build_prod(context: OpExecutionContext, config: ProdBuildConfig, project: str) -> None:
    # A branch deployment runs unmerged code, which must never reach PROD.
    target = "dev" if IS_BRANCH_DEPLOYMENT else "prod"
    if target == "dev":
        context.log.warning("branch deployment: building DEV, not PROD")
    args = ["build", "--target", target]
    if config.select:
        args += ["--select", config.select]
    if config.full_refresh:
        args += ["--full-refresh"]
    run_dbt(context, Path(project), *args)


@op
def dbt_resync_dev_op(context: OpExecutionContext, project: str) -> None:
    run_dbt(context, Path(project), "run-operation", "resync_dev_from_prod", "--target", "dev")


@job(executor_def=in_process_executor, tags=CHECKOUT_TAG)
def dbt_prod_build():
    dbt_build_prod(checkout_main())


@job(executor_def=in_process_executor, tags=CHECKOUT_TAG)
def dbt_resync_dev():
    dbt_resync_dev_op(checkout_main())


@run_status_sensor(run_status=DagsterRunStatus.SUCCESS, monitored_jobs=[dbt_prod_build],
                   request_job=dbt_resync_dev, default_status=SENSOR_STATUS)
def resync_dev(context: RunStatusSensorContext) -> RunRequest:
    """DEV = PROD once a PROD build has succeeded; one resync per build."""
    return RunRequest(run_key=context.dagster_run.run_id)


CRAWLS = [tapology_crawl, ufcstats_crawl, mmadecisions_crawl]
IP_RETRY_TAG = "fightgrade/ip_retry"
# With one refused IP in three, four misses in a row is about one night in 80.
MAX_IP_RETRIES = 3


def failure_text(event) -> str:
    data = event.step_failure_data
    return " ".join(filter(None, [data.error.message if data.error else None,
                                  data.user_failure_data.description if data.user_failure_data else None]))


@run_failure_sensor(monitored_jobs=CRAWLS, request_jobs=CRAWLS, default_status=SENSOR_STATUS)
def crawl_retry(context: RunFailureSensorContext):
    """Launch a crawl again, same config, when it stopped before archiving anything:
    the new run gets its own egress IP, usually another one. Any other failure is
    left alone, since a failed check would fail again and re-crawl on the way."""
    run = context.dagster_run
    if not any(RETRY_ON_ANOTHER_IP in failure_text(e) for e in context.get_step_failure_events()):
        return SkipReason(f"{run.run_id[:8]} failed after fetching, or for another reason: not retried")
    tries = int(run.tags.get(IP_RETRY_TAG, "0"))
    if tries >= MAX_IP_RETRIES:
        return SkipReason(f"{run.run_id[:8]} was already retry {tries} of {MAX_IP_RETRIES}")
    return RunRequest(run_key=f"{run.run_id}-ip-retry", job_name=run.job_name, run_config=run.run_config,
                      tags={IP_RETRY_TAG: str(tries + 1)})


defs = Definitions(
    jobs=[dbt_prod_build, dbt_resync_dev, *CRAWLS],
    schedules=[
        ScheduleDefinition(
            job=dbt_prod_build,
            cron_schedule="0 3 * * *",
            execution_timezone=TIMEZONE,
            default_status=SCHEDULE_STATUS,
        ),
        ScheduleDefinition(
            job=tapology_crawl,
            cron_schedule=DAILY_0200,
            execution_timezone=TIMEZONE,
            default_status=SCHEDULE_STATUS,
        ),
        ScheduleDefinition(
            job=ufcstats_crawl,
            cron_schedule=SUNDAY_0600,
            execution_timezone=TIMEZONE,
            default_status=SCHEDULE_STATUS,
        ),
        ScheduleDefinition(
            job=mmadecisions_crawl,
            cron_schedule=SUNDAY_0600,
            execution_timezone=TIMEZONE,
            default_status=SCHEDULE_STATUS,
        ),
    ],
    sensors=[resync_dev, crawl_retry],
)
