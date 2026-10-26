#!/usr/bin/env bash
# Dagster UI on http://localhost:3000, plus the daemon that fires the schedules.
# Schedules only fire while this is running (and the Mac is awake).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export DAGSTER_HOME="${DAGSTER_HOME:-$HOME/.fightgrade/dagster}"
mkdir -p "$DAGSTER_HOME"
cp "$ROOT/orchestration/dagster.yaml" "$DAGSTER_HOME/dagster.yaml"

cd "$ROOT"
exec "$ROOT/.venv/bin/dagster" dev -m orchestration "$@"
