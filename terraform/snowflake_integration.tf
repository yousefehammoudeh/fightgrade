# ─────────────────────────────────────────────────────────────────────
# Snowflake ↔ GCS. On GCS the integration is one-directional:
#
#   1. the storage integration emits a Snowflake-managed service account
#   2. grant that service account read on the bucket
#
# No external ID, no trust policy, no circular bootstrap — that was AWS-specific
# (DESIGN.md §11). Terraform earns its keep here on reproducibility, review and
# teardown, not on dependency resolution.
# ─────────────────────────────────────────────────────────────────────

resource "snowflake_storage_integration_gcs" "lake" {
  name                      = "FIGHTGRADE_LAKE"
  enabled                   = true
  storage_allowed_locations = ["gcs://${google_storage_bucket.lake.name}/parsed/"]
}

# Exactly the three permissions Snowflake needs to load: nothing to write or delete.
resource "google_project_iam_custom_role" "snowflake_lake_reader" {
  role_id     = "snowflakeLakeReader"
  title       = "Snowflake lake reader"
  permissions = ["storage.buckets.get", "storage.objects.get", "storage.objects.list"]
  depends_on  = [google_project_service.enabled]
}

resource "google_storage_bucket_iam_member" "snowflake_lake_reader" {
  bucket = google_storage_bucket.lake.name
  role   = google_project_iam_custom_role.snowflake_lake_reader.name
  member = "serviceAccount:${snowflake_storage_integration_gcs.lake.describe_output[0].service_account}"
}

# Note the scope: Snowflake reads parsed/ ONLY. The IAM grant is bucket-wide
# (a prefix condition would also block the bucket-level list call Snowflake
# needs), so the restriction lives in storage_allowed_locations — no stage on
# this integration can point at raw-html/.

resource "snowflake_file_format_parquet" "parquet" {
  database         = snowflake_database.fightgrade.name
  schema           = snowflake_schema.layer["LANDING"].name
  name             = "PARQUET_FORMAT"
  use_logical_type = true # DATE and TIMESTAMP columns arrive typed, not as integers
}

resource "snowflake_stage_external_gcs" "lake" {
  database            = snowflake_database.fightgrade.name
  schema              = snowflake_schema.layer["LANDING"].name
  name                = "LAKE_PARSED"
  url                 = "gcs://${google_storage_bucket.lake.name}/parsed/"
  storage_integration = snowflake_storage_integration_gcs.lake.name
  file_format {
    format_name = snowflake_file_format_parquet.parquet.fully_qualified_name
  }
  depends_on = [google_storage_bucket_iam_member.snowflake_lake_reader]
}

resource "snowflake_grant_privileges_to_account_role" "transformer_integration" {
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "INTEGRATION"
    object_name = snowflake_storage_integration_gcs.lake.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "transformer_stage" {
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE"]
  on_schema_object {
    object_type = "STAGE"
    object_name = snowflake_stage_external_gcs.lake.fully_qualified_name
  }
}

resource "snowflake_grant_privileges_to_account_role" "transformer_file_format" {
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE"]
  on_schema_object {
    object_type = "FILE FORMAT"
    object_name = snowflake_file_format_parquet.parquet.fully_qualified_name
  }
}

output "stage" {
  value       = snowflake_stage_external_gcs.lake.fully_qualified_name
  description = "LIST @<this>; should show the 17 Parquet files"
}
