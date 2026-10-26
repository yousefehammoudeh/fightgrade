"""A clean checkout of origin/main: the only code PROD is ever built from.

The prod job never runs your working copy. It fetches origin/main into its own
directory, hard-resets to it and wipes everything untracked, so what lands in
FIGHTGRADE_PROD is exactly what is merged — no uncommitted edits, no local
branch, no stale dbt target/ from a previous run.
"""
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKOUT = Path(os.environ.get("FIGHTGRADE_MAIN_CHECKOUT", "~/.fightgrade/main")).expanduser()


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
