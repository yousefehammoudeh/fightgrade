terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0"
    }
    # NOTE: this provider changed hands twice (community -> Snowflake-Labs ->
    # snowflakedb) and the grant resources were reworked on the way to v1, so
    # check the registry rather than older examples.
    snowflake = {
      source  = "snowflakedb/snowflake"
      version = ">= 1.0"
    }
  }
}

# `terraform init` writes .terraform.lock.hcl with exact versions.
# Commit that lock file; it is what actually pins the providers.

# Credentials come from Application Default Credentials:
#   gcloud auth application-default login
provider "google" {
  project = var.project_id
  region  = var.region

  default_labels = {
    project    = "fightgrade"
    managed-by = "terraform"
  }
}

# Key-pair auth as a service user (no password, no MFA prompt). The user and
# its public key are created by hand once — the bootstrap Terraform can't do
# for itself.
provider "snowflake" {
  organization_name = var.snowflake_organization_name
  account_name      = var.snowflake_account_name
  user              = "TERRAFORM_SVC"
  role              = "ACCOUNTADMIN" # storage integrations and resource monitors need it
  authenticator     = "SNOWFLAKE_JWT"
  private_key       = file(pathexpand(var.snowflake_private_key_path))

  # These resources are still "preview" in the snowflakedb provider and must be
  # opted into explicitly.
  preview_features_enabled = [
    "snowflake_storage_integration_gcs_resource",
    "snowflake_stage_external_gcs_resource",
    "snowflake_file_format_parquet_resource",
  ]
}
