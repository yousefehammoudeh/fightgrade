"""Run the dbt CLI from a checkout and stream its output into the Dagster run log."""
import subprocess
import sys
from pathlib import Path

from dagster import Failure, OpExecutionContext

DBT = Path(sys.executable).parent / "dbt"
PROFILES_DIR = Path("~/.dbt").expanduser()


def dbt_project(checkout: Path) -> Path:
    project = checkout / "dbt"
    if not (project / "dbt_project.yml").exists():
        raise Failure(f"{checkout} has no dbt/ project — it isn't on main yet, so there is nothing to build.")
    return project


def run_dbt(context: OpExecutionContext, project: Path, *args: str) -> None:
    cmd = [str(DBT), "--no-use-colors", *args, "--project-dir", str(project), "--profiles-dir", str(PROFILES_DIR)]
    context.log.info("$ " + " ".join(cmd))
    proc = subprocess.Popen(cmd, cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        context.log.info(line.rstrip())
    if proc.wait():
        raise Failure(f"dbt {args[0]} exited with {proc.returncode}")
