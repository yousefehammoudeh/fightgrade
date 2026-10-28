"""Upload the Judge Lens page and create PUBLISHED.JUDGE_LENS, a Streamlit in Snowflake app.

    .venv/bin/python -m apps.judge_lens.deploy          # DEV, from a laptop

PROD gets it from the judge model's training job (orchestration/judge_lens.py),
built from main. The app runs on FIGHTGRADE_WH with its owner's rights and reads
only the PUBLISHED Judge Lens views.
"""
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "streamlit_app.py"
# the container runtime installs the app's dependencies from this, beside the app
FILES = [APP, HERE / "pyproject.toml"]


def deploy(cur, env: str) -> str:
    cur.execute(f"use database FIGHTGRADE_{env.upper()}")
    cur.execute("create stage if not exists ML.APPS comment = 'Streamlit app sources (apps/)'")
    for f in FILES:
        cur.execute(f"put 'file://{f}' @ML.APPS/judge_lens auto_compress = false overwrite = true")
    cur.execute("""
        create or replace streamlit PUBLISHED.JUDGE_LENS
        from '@ML.APPS/judge_lens'
        main_file = 'streamlit_app.py'
        query_warehouse = FIGHTGRADE_WH
        title = 'Judge Lens'
        comment = 'Judge Lens: what the judges saw, what the stats say (apps/judge_lens)'
    """)
    return f"FIGHTGRADE_{env.upper()}.PUBLISHED.JUDGE_LENS"


if __name__ == "__main__":
    from pipeline.tapology import snowflake_cursor

    print("created", deploy(snowflake_cursor("dev"), "dev"))
