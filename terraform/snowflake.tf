# ─────────────────────────────────────────────────────────────────────
# Snowflake: one warehouse, one database, the five layer schemas, two roles.
#
# Terraform owns containers and grants; dbt owns every table and view inside
# them (DESIGN.md §5). So dbt never creates a schema, and Terraform never
# manages a table — tables dbt builds later inherit SELECT through the
# future grants at the bottom of this file.
# ─────────────────────────────────────────────────────────────────────

# The trial is $400 of credits. Suspend everything well before that, and warn
# on the way up. Notifications go to account admins with notifications enabled.
resource "snowflake_resource_monitor" "fightgrade" {
  name            = "FIGHTGRADE_MONITOR"
  credit_quota    = var.snowflake_credit_quota
  notify_triggers = [50, 75, 90]
  suspend_trigger = 100
}

resource "snowflake_warehouse" "fightgrade" {
  name                = "FIGHTGRADE_WH"
  warehouse_size      = "XSMALL"
  auto_suspend        = 60 # seconds — idle time is the main way a trial burns credits
  auto_resume         = true
  initially_suspended = true
  resource_monitor    = snowflake_resource_monitor.fightgrade.fully_qualified_name
}

resource "snowflake_database" "fightgrade" {
  name                           = "FIGHTGRADE"
  drop_public_schema_on_creation = true
}

locals {
  # STAGING holds dbt intermediate models, used sparingly.
  layers = ["LANDING", "STAGING", "CLEANSING", "REFINED", "PUBLISHED"]
}

resource "snowflake_schema" "layer" {
  for_each = toset(local.layers)
  database = snowflake_database.fightgrade.name
  name     = each.value
}

# ─── Roles ───────────────────────────────────────────────────────────
# TRANSFORMER: what Dagster and dbt run as — loads LANDING, builds every layer.
# READER: what Streamlit runs as — reads PUBLISHED only.

resource "snowflake_account_role" "transformer" {
  name = "FIGHTGRADE_TRANSFORMER"
}

resource "snowflake_account_role" "reader" {
  name = "FIGHTGRADE_READER"
}

# Custom roles roll up to SYSADMIN, so admins can still see and manage what
# they own (Snowflake's recommended hierarchy).
resource "snowflake_grant_account_role" "to_sysadmin" {
  for_each         = { transformer = snowflake_account_role.transformer.name, reader = snowflake_account_role.reader.name }
  role_name        = each.value
  parent_role_name = "SYSADMIN"
}

resource "snowflake_grant_account_role" "transformer_to_user" {
  role_name = snowflake_account_role.transformer.name
  user_name = var.snowflake_user_name
}

resource "snowflake_grant_privileges_to_account_role" "warehouse_usage" {
  for_each          = { transformer = snowflake_account_role.transformer.name, reader = snowflake_account_role.reader.name }
  account_role_name = each.value
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.fightgrade.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "database_usage" {
  for_each          = { transformer = snowflake_account_role.transformer.name, reader = snowflake_account_role.reader.name }
  account_role_name = each.value
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.fightgrade.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "transformer_schemas" {
  for_each          = snowflake_schema.layer
  account_role_name = snowflake_account_role.transformer.name
  privileges        = ["USAGE", "CREATE TABLE", "CREATE VIEW"]
  on_schema {
    schema_name = each.value.fully_qualified_name
  }
}

resource "snowflake_grant_privileges_to_account_role" "reader_published" {
  account_role_name = snowflake_account_role.reader.name
  privileges        = ["USAGE"]
  on_schema {
    schema_name = snowflake_schema.layer["PUBLISHED"].fully_qualified_name
  }
}

# The future grants: a table dbt creates in PUBLISHED tomorrow is readable by
# Streamlit without Terraform ever knowing it exists.
resource "snowflake_grant_privileges_to_account_role" "reader_future" {
  for_each          = toset(["TABLES", "VIEWS"])
  account_role_name = snowflake_account_role.reader.name
  privileges        = ["SELECT"]
  on_schema_object {
    future {
      object_type_plural = each.value
      in_schema          = snowflake_schema.layer["PUBLISHED"].fully_qualified_name
    }
  }
}
