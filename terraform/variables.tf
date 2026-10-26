variable "project_id" {
  description = "GCP project ID (not the display name). Set it in terraform.tfvars."
  type        = string

  # The example file's placeholder resolves to someone else's project and fails
  # with a confusing 403 on the API-enable step.
  validation {
    condition     = var.project_id != "your-project-id"
    error_message = "Set project_id in terraform.tfvars to your real GCP project ID."
  }
}

variable "region" {
  description = "Bucket region. The Snowflake account is on AWS_US_EAST_2 and reads it cross-cloud (GCS egress, ~$0.12/GB — cents at this volume)."
  type        = string
  default     = "us-central1"
}

variable "bucket_name" {
  description = "Globally unique. GCS bucket names are shared across all GCP projects."
  type        = string
  default     = "fightgrade-lake"
}

variable "snowflake_organization_name" {
  description = "First half of the account identifier: SELECT CURRENT_ORGANIZATION_NAME();"
  type        = string
}

variable "snowflake_account_name" {
  description = "Second half of the account identifier: SELECT CURRENT_ACCOUNT_NAME();"
  type        = string
}

variable "snowflake_user_name" {
  description = "Your own Snowflake login, granted FIGHTGRADE_TRANSFORMER so you can run dbt as it."
  type        = string
}

variable "snowflake_private_key_path" {
  description = "Unencrypted PKCS#8 private key for TERRAFORM_SVC. Keep it outside the repo."
  type        = string
  default     = "~/.snowflake/keys/terraform_svc.p8"
}

variable "snowflake_credit_quota" {
  description = "Credits before every warehouse is suspended. Aim for ~$300 of the $400 trial: ~$2/credit on Standard (150), ~$3 on Enterprise (100)."
  type        = number
  default     = 100
}

variable "raw_html_nearline_after_days" {
  description = "Raw HTML is cold after the parse; keep it cheap but instantly retrievable for replay."
  type        = number
  default     = 90
}
