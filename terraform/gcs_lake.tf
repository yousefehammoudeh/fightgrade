# ─────────────────────────────────────────────────────────────────────
# The lake: one bucket, two zones.
#   raw-html/   immutable archive — the only thing that cannot be recomputed
#   parsed/     Parquet — what Snowflake actually ingests
# ─────────────────────────────────────────────────────────────────────

resource "google_project_service" "enabled" {
  for_each = toset([
    "storage.googleapis.com",
    "secretmanager.googleapis.com", # Snowflake key-pair auth, DESIGN.md §4.3
    "iam.googleapis.com",           # the custom role Snowflake reads the bucket with
  ])
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "lake" {
  name     = var.bucket_name
  location = upper(var.region)

  # IAM only, no per-object ACLs. Required for IAM conditions, and the
  # public-access block in the S3 version becomes one line here.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # The archive is the system of record for 170k scraped pages. Versioning is the
  # guard against a `gcloud storage rsync --delete-unmatched-destination-objects`
  # or a bad key transform wiping it.
  versioning {
    enabled = true
  }

  # GCS adds a 7-day soft-delete window by default, billed as storage. Versioning
  # already covers recovery, so paying for both buys nothing.
  soft_delete_policy {
    retention_duration_seconds = 0
  }

  # Raw HTML is read once at parse time, then only for replay. Nearline keeps a
  # re-parse instant (no restore step) while halving storage cost.
  lifecycle_rule {
    condition {
      age            = var.raw_html_nearline_after_days
      matches_prefix = ["raw-html/"]
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  # Versioning is a safety net, not a history: expire old versions.
  lifecycle_rule {
    condition {
      days_since_noncurrent_time = 30
    }
    action {
      type = "Delete"
    }
  }

  # Multipart leftovers from a failed upload are invisible and billable.
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  # Encryption at rest is Google-managed by default; no resource needed.

  depends_on = [google_project_service.enabled]
}

output "bucket" {
  value       = google_storage_bucket.lake.name
  description = "gcloud storage rsync -r _s3stage/ gs://<this>/"
}

output "bucket_url" {
  value = google_storage_bucket.lake.url
}
