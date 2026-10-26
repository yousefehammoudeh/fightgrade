"""Where a run gets its code: always main, never a working copy.

On Dagster+ the deployed code location *is* main: every push to main redeploys
it (`.github/workflows/`), so the dbt project bundled with the package is used
as-is. Branch deployments carry their PR's code instead and are pointed at DEV.

Locally there is no deploy, so a run fetches origin/main into its own
directory, hard-resets to it and wipes everything untracked. What lands in
FIGHTGRADE_PROD is exactly what is merged — no uncommitted edits, no local
branch, no stale dbt target/ from a previous run.
"""
import os
import subprocess
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
CHECKOUT = Path(os.environ.get("FIGHTGRADE_MAIN_CHECKOUT", "~/.fightgrade/main")).expanduser()

IN_DAGSTER_PLUS = "DAGSTER_CLOUD_DEPLOYMENT_NAME" in os.environ
IS_BRANCH_DEPLOYMENT = os.environ.get("DAGSTER_CLOUD_IS_BRANCH_DEPLOYMENT") == "1"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def sync_main() -> str:
    """Bring CHECKOUT to exactly origin/main and return its commit SHA."""
    if not (CHECKOUT / ".git").exists():
        url = _git("remote", "get-url", "origin", cwd=REPO_ROOT)
        CHECKOUT.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--quiet", url, str(CHECKOUT), cwd=CHECKOUT.parent)
    _git("fetch", "--quiet", "origin", "main", cwd=CHECKOUT)
    _git("checkout", "--quiet", "--detach", "origin/main", cwd=CHECKOUT)
    _git("reset", "--quiet", "--hard", "origin/main", cwd=CHECKOUT)
    _git("clean", "--quiet", "-fdx", cwd=CHECKOUT)
    return _git("rev-parse", "HEAD", cwd=CHECKOUT)


def bundled_dbt_project() -> Path:
    # Installed from the wheel, pyproject.toml force-includes dbt/ here; run
    # from source, it is still at the repo root.
    for candidate in (PACKAGE_DIR / "dbt_project", REPO_ROOT / "dbt"):
        if (candidate / "dbt_project.yml").exists():
            return candidate
    raise FileNotFoundError("no dbt project bundled with this code location")


def main_dbt_project() -> tuple[Path, str]:
    """The dbt project to run, and the commit (or deploy) it came from."""
    if IN_DAGSTER_PLUS:
        return bundled_dbt_project(), os.environ.get("DAGSTER_CLOUD_GIT_SHA", "deployed main")
    sha = sync_main()
    return CHECKOUT / "dbt", sha
