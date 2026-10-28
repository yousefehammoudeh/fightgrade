"""Judge Lens's per-fight probabilities from DEV, for the Elo prototype.

    python3 experiments/elo_mma/pull_lens.py   # -> data/elo/judge_lens_fights.parquet

Read-only: one select on FIGHTGRADE_DEV.PUBLISHED.PUB_JUDGE_LENS as DBT_DEV_SVC.
"""
import os
from pathlib import Path

import snowflake.connector

OUT = Path(__file__).resolve().parents[2] / "data" / "elo" / "judge_lens_fights.parquet"

conn = snowflake.connector.connect(
    account="FNMYVFY-FP38730", user="DBT_DEV_SVC",
    private_key_file=os.path.expanduser("~/.snowflake/keys/dbt_dev_svc.p8"),
    role="FIGHTGRADE_TRANSFORMER_DEV", warehouse="FIGHTGRADE_WH", database="FIGHTGRADE_DEV")
df = conn.cursor().execute("""
    select ufcstats_fight_id, event_date, fighter_a, fighter_b, result, trained_on,
           p_a_wins, p_b_wins, average_panel_p_a_wins, average_panel_p_b_wins, model_version
    from published.pub_judge_lens
""").fetch_pandas_all()
df.columns = [c.lower() for c in df.columns]
df.to_parquet(OUT, index=False)
print(f"{len(df):,} decisions, {df.event_date.min()} .. {df.event_date.max()}, version {df.model_version.iloc[0]} -> {OUT}")
