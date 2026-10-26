"""Run dbt in-process and stream its log into the Dagster run.

dbt is invoked through its Python API rather than a `dbt` executable: a Dagster+
code location runs from a packaged environment with no console scripts on PATH.
"""
import tempfile
from pathlib import Path

from dagster import Failure, OpExecutionContext
from dbt.cli.main import dbtRunner

from .main_checkout import IN_DAGSTER_PLUS, PACKAGE_DIR

# Dagster+ reads credentials from deployment environment variables; locally
# they come from ~/.dbt/profiles.yml and the key files beside it.
PROFILES_DIR = PACKAGE_DIR / "dbt_profiles" if IN_DAGSTER_PLUS else Path("~/.dbt").expanduser()


def run_dbt(context: OpExecutionContext, project: Path, *args: str) -> None:
    if not (project / "dbt_project.yml").exists():
        raise Failure(f"{project} has no dbt project — it isn't on main yet, so there is nothing to build.")

    # target/ and logs/ go to a scratch directory: the project may sit in a
    # read-only package, and nothing from a previous run should leak in.
    scratch = Path(tempfile.mkdtemp(prefix="dbt-"))
    cli_args = [
        "--no-use-colors", *args,
        "--project-dir", str(project),
        "--profiles-dir", str(PROFILES_DIR),
        "--target-path", str(scratch / "target"),
        "--log-path", str(scratch / "logs"),
    ]
    context.log.info("dbt " + " ".join(cli_args))

    def forward(event) -> None:
        if event.info.level in ("info", "warn", "error") and event.info.msg:
            context.log.info(event.info.msg)

    result = dbtRunner(callbacks=[forward]).invoke(cli_args)
    if not result.success:
        raise Failure(f"dbt {args[0]} failed" + (f": {result.exception}" if result.exception else ""))
