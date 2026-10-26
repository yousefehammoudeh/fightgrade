"""Dagster definitions for FightGrade.

    orchestration/run_dagster.sh      UI on http://localhost:3000 + the schedule daemon

Two jobs, both built from a clean checkout of origin/main (main_checkout.py):

  dbt_prod_build   PROD = main. Daily at 03:00, or launched by hand from the UI
                   for a rebuild now — optionally narrowed with `select`.
  dbt_resync_dev   DEV = PROD. Weekly, Sunday 04:00, after that night's build.
                   Drops DEV-only tables, so it is weekly rather than daily.
"""
from dagster import (
    Config,
    DefaultScheduleStatus,
    Definitions,
    OpExecutionContext,
    ScheduleDefinition,
    in_process_executor,
    job,
    op,
)

from .dbt_cli import dbt_project, run_dbt
from .main_checkout import CHECKOUT, sync_main

TIMEZONE = "America/Toronto"

# Both jobs reset the same checkout; dagster.yaml queues them so they never overlap.
CHECKOUT_TAG = {"fightgrade/checkout": "main"}


@op
def checkout_main(context: OpExecutionContext) -> str:
    sha = sync_main()
    context.log.info(f"building from origin/main at {sha[:7]}")
    context.add_output_metadata({"commit": sha, "checkout": str(CHECKOUT)})
    return sha


class ProdBuildConfig(Config):
    # Empty builds everything on main. A dbt selector ("landing",
    # "lnd_tapology_bouts+") narrows a manual run.
    select: str = ""


@op
def dbt_build_prod(context: OpExecutionContext, config: ProdBuildConfig, sha: str) -> None:
    args = ["build", "--target", "prod"]
    if config.select:
        args += ["--select", config.select]
    run_dbt(context, dbt_project(CHECKOUT), *args)


@op
def dbt_resync_dev_op(context: OpExecutionContext, sha: str) -> None:
    run_dbt(context, dbt_project(CHECKOUT), "run-operation", "resync_dev_from_prod", "--target", "dev")


@job(executor_def=in_process_executor, tags=CHECKOUT_TAG)
def dbt_prod_build():
    dbt_build_prod(checkout_main())


@job(executor_def=in_process_executor, tags=CHECKOUT_TAG)
def dbt_resync_dev():
    dbt_resync_dev_op(checkout_main())


defs = Definitions(
    jobs=[dbt_prod_build, dbt_resync_dev],
    schedules=[
        ScheduleDefinition(
            job=dbt_prod_build,
            cron_schedule="0 3 * * *",
            execution_timezone=TIMEZONE,
            default_status=DefaultScheduleStatus.RUNNING,
        ),
        ScheduleDefinition(
            job=dbt_resync_dev,
            cron_schedule="0 4 * * 0",
            execution_timezone=TIMEZONE,
            default_status=DefaultScheduleStatus.RUNNING,
        ),
    ],
)
