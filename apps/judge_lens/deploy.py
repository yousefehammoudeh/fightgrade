"""Upload the Judge Lens page and create PUBLISHED.JUDGE_LENS, a Streamlit in Snowflake app.

    .venv/bin/python -m apps.judge_lens.deploy          # DEV, from a laptop

PROD gets it from the judge model's training job (orchestration/judge_lens.py),
built from main. The app runs on FIGHTGRADE_WH with its owner's rights and reads
only the PUBLISHED Judge Lens views.

The runtime is named, never left to Snowflake: left unset, a new app can land on
the container runtime, which installs dependencies from PyPI, and this account
has no external access to it (PROD's first app failed that way). The warehouse
runtime ships Streamlit and Snowpark, all the page needs, and fetches nothing.
"""
from pathlib import Path

APP = Path(__file__).resolve().parent / "streamlit_app.py"
RUNTIME = "SYSTEM$WAREHOUSE_RUNTIME"


def deploy(cur, env: str) -> str:
    cur.execute(f"use database FIGHTGRADE_{env.upper()}")
    cur.execute("create stage if not exists ML.APPS comment = 'Streamlit app sources (apps/)'")
    cur.execute("remove @ML.APPS/judge_lens")         # only what is uploaded now becomes the app
    cur.execute(f"put 'file://{APP}' @ML.APPS/judge_lens auto_compress = false overwrite = true")
    cur.execute(f"""
        create or replace streamlit PUBLISHED.JUDGE_LENS
        from '@ML.APPS/judge_lens'
        main_file = 'streamlit_app.py'
        query_warehouse = FIGHTGRADE_WH
        runtime_name = '{RUNTIME}'
        title = 'Judge Lens'
        comment = 'Judge Lens: what the judges saw, what the stats say (apps/judge_lens)'
    """)
    return f"FIGHTGRADE_{env.upper()}.PUBLISHED.JUDGE_LENS"


if __name__ == "__main__":
    from pipeline.tapology import snowflake_cursor

    print("created", deploy(snowflake_cursor("dev"), "dev"))
