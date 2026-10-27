"""judge_lens_train: the judge model's retrain and prospective test (DESIGN.md §8).

    judge_lens_deploy -> judge_lens_log -> judge_lens_fit

After every successful dbt_prod_build (the judge_lens_after_build sensor), so the
REFINED facts it reads hold the newest decisions:

    deploy  ML.TRAIN_JUDGE_LENS from this code location's ml/judge_lens (main, on
            Dagster+); the model's checks run inside the procedure, since this
            image has no numpy or scipy
    log     the default version scores every round newer than what it trained on,
            into ML.JUDGE_LENS_PREDICTIONS: a new card is scored by the version
            that existed before it
    fit     retrain on every usable round with the chosen settings, backtest the
            newest year, register a version of ML.JUDGE_LENS, and make it the
            default only if the gate passes. With no new rounds it does nothing.

All the work is in the procedure, on FIGHTGRADE_WH; this job only calls it.
"""
from dagster import Failure, OpExecutionContext, in_process_executor, job, op

from ml.judge_lens.deploy import call, deploy
from pipeline.lake import environment
from pipeline.tapology import snowflake_cursor


def _cursor(env: str):
    cur = snowflake_cursor(env)
    cur.execute(f"use database FIGHTGRADE_{env.upper()}")
    cur.execute("alter session set statement_timeout_in_seconds = 3600")
    return cur


@op
def judge_lens_deploy(context: OpExecutionContext) -> str:
    env = environment()
    deploy(_cursor(env), env, check=False)
    context.log.info(f"created FIGHTGRADE_{env.upper()}.ML.TRAIN_JUDGE_LENS")
    return env


@op
def judge_lens_log(context: OpExecutionContext, env: str) -> str:
    out = call(_cursor(env), {"mode": "log"})
    context.log.info(f"prediction log: {out}")
    context.add_output_metadata({k: str(v) for k, v in out.items()})
    return env


@op
def judge_lens_fit(context: OpExecutionContext, env: str) -> None:
    out = call(_cursor(env), {"mode": "fit"})
    summary = {k: v for k, v in out.items() if k != "metrics"}
    context.log.info(f"fit: {summary}")
    context.add_output_metadata({k: str(v) for k, v in summary.items()})
    if out.get("registered") and not out.get("promoted"):
        # registered for the record, but the default stays where it was
        raise Failure(f"{out['version']} failed the gate and was not made the default: {out['gate_failures']}")


@job(executor_def=in_process_executor)
def judge_lens_train():
    judge_lens_fit(judge_lens_log(judge_lens_deploy()))
