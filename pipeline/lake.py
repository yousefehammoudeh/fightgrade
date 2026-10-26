"""The lake: where raw HTML and parsed Parquet land, per environment.

    gs://fightgrade-lake/{zone}raw-html/source=<s>/entity=<e>/fetch_date=<d>/<slug>.html.gz
    gs://fightgrade-lake/{zone}parsed/entity=<table>/fetch_date=<d>/part-<run_id>-<i>.parquet

The zone is "" for PROD and "dev/" for DEV, mirroring the Snowflake split: each
environment's stage reads only its own zone (terraform/snowflake_integration.tf),
so a test crawl can never reach PROD's 03:00 build.

The environment defaults to DEV everywhere except the Dagster+ production
deployment. FIGHTGRADE_ENV=prod overrides it, for a deliberate PROD run from a
machine that can crawl (Tapology needs headed Chrome).

Parquet files are named per run, never `data_0.parquet`: LANDING skips files it
has already loaded *by name*, so a rewritten partition must arrive as a new file
or its corrections are never loaded. CLEANSING keeps the latest copy of each row.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

BUCKET = os.environ.get("FIGHTGRADE_BUCKET", "fightgrade-lake")
PROJECT = os.environ.get("FIGHTGRADE_GCP_PROJECT", "fightgrade-yh26")


def environment() -> str:
    explicit = os.environ.get("FIGHTGRADE_ENV")
    if explicit:
        if explicit not in ("dev", "prod"):
            raise ValueError(f"FIGHTGRADE_ENV must be dev or prod, not {explicit!r}")
        return explicit
    on_prod_deployment = (os.environ.get("DAGSTER_CLOUD_DEPLOYMENT_NAME") == "prod"
                          and os.environ.get("DAGSTER_CLOUD_IS_BRANCH_DEPLOYMENT") != "1")
    return "prod" if on_prod_deployment else "dev"


def zone(env: str | None = None) -> str:
    return "" if (env or environment()) == "prod" else "dev/"


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def raw_html_key(source: str, entity: str, fetch_date: str, slug: str, env: str | None = None) -> str:
    return f"{zone(env)}raw-html/source={source}/entity={entity}/fetch_date={fetch_date}/{slug}.html.gz"


# Every parsed/ Parquet file has exactly these three columns. `_json` is the row
# the extractor produced, as its exact JSON text; Snowflake turns it into the
# VARIANT payload with parse_json, so no type is ever *guessed*. Inferring a
# schema per batch made the same field differ between partitions: "2:42" (a
# finish time) became a clock time in a small batch, and dynamic-key objects
# came out as a MAP in the bulk load but a STRUCT in a small one. The lake's own
# fields stay out of `_json`, so the payload is the source row and nothing else.
JSON_ROW_COLUMNS = "{'_json': 'VARCHAR', '_raw_html_key': 'VARCHAR', 'fetch_date': 'DATE'}"


def write_json_rows(path: Path, items) -> None:
    """items: (row, raw_html_key, fetch_date) -> one wrapped JSONL line each."""
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row, key, fetch_date in items:
            f.write(json.dumps({"_json": json.dumps(row, ensure_ascii=False),
                                "_raw_html_key": key, "fetch_date": fetch_date}, ensure_ascii=False) + "\n")


def json_rows_select(path: Path) -> str:
    return (f"SELECT * FROM read_json('{path}', format='newline_delimited', "
            f"columns={JSON_ROW_COLUMNS})")


def jsonl_lines(path: Path) -> str:
    """A JSONL file as one VARCHAR column of exact line text (no parsing, no inference).
    Ctrl-A as the delimiter is safe: JSON escapes every control character."""
    return (f"read_csv('{path}', columns={{'_json': 'VARCHAR'}}, header=false, "
            f"delim=chr(1), quote='', escape='', auto_detect=false)")


def parquet_copy_options(run_id: str) -> str:
    """DuckDB COPY options for one table's partitions in the parsed/ layout."""
    return ("FORMAT parquet, COMPRESSION zstd, PARTITION_BY (fetch_date), "
            f"WRITE_PARTITION_COLUMNS true, FILENAME_PATTERN 'part-{run_id}-{{i}}'")


class Lake:
    """Thin wrapper over the bucket.

    Credentials: on Dagster+, the `fightgrade-lake-writer` service account's key,
    base64-encoded in the FIGHTGRADE_GCP_SA_KEY deployment variable. Locally,
    Application Default Credentials (`gcloud auth application-default login`)."""

    def __init__(self, env: str | None = None):
        import base64, json
        from google.cloud import storage   # imported here so parsing code needs no GCP libs

        self.env = env or environment()
        key = os.environ.get("FIGHTGRADE_GCP_SA_KEY")
        if key:
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_info(json.loads(base64.b64decode(key)))
            client = storage.Client(project=PROJECT, credentials=creds)
        else:
            client = storage.Client(project=PROJECT)
        self.bucket = client.bucket(BUCKET)

    def upload_file(self, local: Path, key: str, content_type: str) -> None:
        self.bucket.blob(key).upload_from_filename(str(local), content_type=content_type)

    def size(self, key: str) -> int | None:
        blob = self.bucket.get_blob(key)
        return None if blob is None else blob.size

    def archived_slugs(self, source: str, entity: str, zones: tuple[str, ...]) -> set[str]:
        """Slugs with a raw page in any of the given zones — what not to fetch again."""
        slugs = set()
        for z in zones:
            for blob in self.bucket.list_blobs(prefix=f"{z}raw-html/source={source}/entity={entity}/"):
                name = blob.name.rsplit("/", 1)[-1]
                if name.endswith(".html.gz"):
                    slugs.add(name[: -len(".html.gz")])
        return slugs
