"""Dagster definitions for FightGrade.

Deployed to Dagster+ Serverless from main (see .github/workflows/), or run
locally for development with:

    orchestration/run_dagster.sh      UI on http://localhost:3000

Two jobs, both built from main (main_checkout.py):

  dbt_prod_build   PROD = main. Daily at 03:00, or launched by hand from the UI
                   for a rebuild now — optionally narrowed with `select`.
  dbt_resync_dev   DEV = PROD. Weekly, Sunday 04:00, after that night's build.
                   Drops DEV-only tables, so it is weekly rather than daily.

Schedules run only on the Dagster+ production deployment. Locally they start
stopped, so a laptop never races the cloud for the same 03:00 build, and a
branch deployment builds DEV instead of PROD.
"""
from pathlib import Path

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

from .dbt_cli import run_dbt
from .main_checkout import IN_DAGSTER_PLUS, IS_BRANCH_DEPLOYMENT, main_dbt_project

TIMEZONE = "America/Toronto"
SCHEDULE_STATUS = (
    DefaultScheduleStatus.RUNNING if IN_DAGSTER_PLUS and not IS_BRANCH_DEPLOYMENT else DefaultScheduleStatus.STOPPED
)

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


@op
def dbt_build_prod(context: OpExecutionContext, config: ProdBuildConfig, project: str) -> None:
    # A branch deployment runs unmerged code, which must never reach PROD.
    target = "dev" if IS_BRANCH_DEPLOYMENT else "prod"
    if target == "dev":
        context.log.warning("branch deployment: building DEV, not PROD")
    args = ["build", "--target", target]
    if config.select:
        args += ["--select", config.select]
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


defs = Definitions(
    jobs=[dbt_prod_build, dbt_resync_dev],
    schedules=[
        ScheduleDefinition(
            job=dbt_prod_build,
            cron_schedule="0 3 * * *",
            execution_timezone=TIMEZONE,
            default_status=SCHEDULE_STATUS,
        ),
        ScheduleDefinition(
            job=dbt_resync_dev,
            cron_schedule="0 4 * * 0",
            execution_timezone=TIMEZONE,
            default_status=SCHEDULE_STATUS,
        ),
    ],
)
