"""Zip this package to @ML.CODE and create the ML.TRAIN_JUDGE_LENS procedure.

    .venv/bin/python -m ml.judge_lens.deploy
    .venv/bin/python -m ml.judge_lens.deploy --call '{"mode": "cv", "configs": [{"rank": 0}, {"rank": 2}]}'

DEV only from a laptop: PROD gets the procedure from the training job, built from
main. The procedure runs as its caller, on FIGHTGRADE_WH. Snowflake installs its
packages from PyPI, pinned to what ml/requirements.txt installs locally. The
model's checks (checks.py) run before anything is uploaded.
"""
import json
import tempfile
import zipfile
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent
RUNTIME = "3.13"
PACKAGES = ["snowflake-snowpark-python", "snowflake-ml-python==2.6.0", "pandas==2.3.3", "numpy==2.5.4",
            "scipy==1.18.1", "scikit-learn==1.9.1"]
PROCEDURE = "ML.TRAIN_JUDGE_LENS"


def deploy(cur, env: str, check: bool = True) -> None:
    """Check the model, upload the package and (re)create the procedure in FIGHTGRADE_<ENV>.
    The training job passes check=False: its image has no numpy or scipy, and mode
    'fit' runs the same checks inside the procedure."""
    if check:
        from .checks import run_all     # numpy and scipy: a laptop's, not the Dagster image's
        run_all()
    cur.execute(f"use database FIGHTGRADE_{env.upper()}")
    cur.execute("create stage if not exists ML.CODE comment = 'Judge Lens training code (ml/judge_lens)'")
    with tempfile.TemporaryDirectory() as tmp:
        zipped = Path(tmp) / "judge_lens.zip"
        with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(PACKAGE.glob("*.py")):
                z.write(f, f"judge_lens/{f.name}")
        cur.execute(f"put 'file://{zipped}' @ML.CODE auto_compress = false overwrite = true")
    packages = ", ".join(f"'{p}'" for p in PACKAGES)
    cur.execute(f"""
        create or replace procedure {PROCEDURE}(config variant)
        returns variant
        language python
        runtime_version = '{RUNTIME}'
        packages = ({packages})
        imports = ('@ML.CODE/judge_lens.zip')
        handler = 'judge_lens.train.main'
        comment = 'Judge Lens: cross-validation, forward check or locked test, logged to Snowflake Experiments'
        execute as caller
    """)


def call(cur, config: dict) -> dict:
    cur.execute(f"call {PROCEDURE}(parse_json(%s))", (json.dumps(config),))
    return json.loads(cur.fetchone()[0])


if __name__ == "__main__":
    import argparse

    from pipeline.tapology import snowflake_cursor

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--call", help="JSON config to run once the procedure is created")
    args = ap.parse_args()
    cur = snowflake_cursor("dev")
    deploy(cur, "dev")
    print(f"created FIGHTGRADE_DEV.{PROCEDURE}")
    if args.call:
        print(json.dumps(call(cur, json.loads(args.call)), indent=1))
