# ─────────────────────────────────────────────────────────────────────
# Snowflake: one warehouse, two databases (DEV and PROD), the five layer
# schemas in each, and the roles that keep them apart.
#
#   FIGHTGRADE_PROD  what main builds. Written only by DBT_PROD_SVC.
#   FIGHTGRADE_DEV   what you edit. `dbt run` locally writes here, and it is
#                    periodically reset to a zero-copy clone of PROD.
#
# Terraform owns containers and grants; dbt owns every table and view inside
# them (DESIGN.md §5). So dbt never creates a schema, and Terraform never
# manages a table — tables dbt builds later inherit privileges through the
# future grants below.
# ─────────────────────────────────────────────────────────────────────

# The trial is $400 of credits. Suspend everything well before that, and warn
# on the way up. Notifications go to account admins with notifications enabled.
resource "snowflake_resource_monitor" "fightgrade" {
  name            = "FIGHTGRADE_MONITOR"
  credit_quota    = var.snowflake_credit_quota
  notify_triggers = [50, 75, 90]
  suspend_trigger = 100
}

# One warehouse for both environments: at this volume a second one only adds
# idle time to pay for.
resource "snowflake_warehouse" "fightgrade" {
  name                = "FIGHTGRADE_WH"
  warehouse_size      = "XSMALL"
  auto_suspend        = 60 # seconds — idle time is the main way a trial burns credits
  auto_resume         = true
  initially_suspended = true
  resource_monitor    = snowflake_resource_monitor.fightgrade.fully_qualified_name
}

locals {
  envs = ["DEV", "PROD"]
  # STAGING holds dbt intermediate models, used sparingly.
  layers = ["LANDING", "STAGING", "CLEANSING", "REFINED", "PUBLISHED"]

  env_schemas  = { for p in setproduct(local.envs, local.layers) : "${p[0]}.${p[1]}" => { env = p[0], layer = p[1] } }
  prod_schemas = { for k, v in local.env_schemas : k => v if v.env == "PROD" }
}

resource "snowflake_database" "env" {
  for_each                       = toset(local.envs)
  name                           = "FIGHTGRADE_${each.key}"
  drop_public_schema_on_creation = true
}

resource "snowflake_schema" "layer" {
  for_each = local.env_schemas
  database = snowflake_database.env[each.value.env].name
  name     = each.value.layer
}

# ─── Roles ───────────────────────────────────────────────────────────
# TRANSFORMER_PROD: builds PROD. Granted only to DBT_PROD_SVC.
# TRANSFORMER_DEV:  builds DEV, and can READ PROD — enough to clone it, never
#                   enough to change it. Granted to you and DBT_DEV_SVC.
# READER:           what Streamlit runs as — reads PROD.PUBLISHED only.

resource "snowflake_account_role" "transformer" {
  for_each = toset(local.envs)
  name     = "FIGHTGRADE_TRANSFORMER_${each.key}"
}

resource "snowflake_account_role" "reader" {
  name = "FIGHTGRADE_READER"
}

locals {
  all_roles = merge(
    { for e in local.envs : "TRANSFORMER_${e}" => snowflake_account_role.transformer[e].name },
    { READER = snowflake_account_role.reader.name },
  )
}

# Custom roles roll up to SYSADMIN, so admins can still see and manage what
# they own (Snowflake's recommended hierarchy).
resource "snowflake_grant_account_role" "to_sysadmin" {
  for_each         = local.all_roles
  role_name        = each.value
  parent_role_name = "SYSADMIN"
}

resource "snowflake_grant_account_role" "dev_to_user" {
  role_name = snowflake_account_role.transformer["DEV"].name
  user_name = var.snowflake_user_name
}

# What dbt (and later Dagster) logs in as: key pair only, one environment each.
# Tables they build are owned by the matching TRANSFORMER role, not ACCOUNTADMIN.
resource "snowflake_service_user" "dbt" {
  for_each          = toset(local.envs)
  name              = "DBT_${each.key}_SVC"
  comment           = "dbt and Dagster for FightGrade ${each.key}"
  rsa_public_key    = join("", [for l in split("\n", trimspace(file(pathexpand(var.dbt_public_key_paths[each.key])))) : l if !startswith(l, "-----")])
  default_role      = snowflake_account_role.transformer[each.key].name
  default_warehouse = snowflake_warehouse.fightgrade.name
  default_namespace = snowflake_database.env[each.key].name
}

resource "snowflake_grant_account_role" "transformer_to_dbt" {
  for_each  = toset(local.envs)
  role_name = snowflake_account_role.transformer[each.key].name
  user_name = snowflake_service_user.dbt[each.key].name
}

# ─── Grants ──────────────────────────────────────────────────────────

resource "snowflake_grant_privileges_to_account_role" "warehouse_usage" {
  for_each          = local.all_roles
  account_role_name = each.value
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "WAREHOUSE"
    object_name = snowflake_warehouse.fightgrade.name
  }
}

resource "snowflake_grant_privileges_to_account_role" "database_usage" {
  for_each = {
    "TRANSFORMER_DEV>DEV"   = { role = "TRANSFORMER_DEV", env = "DEV" }
    "TRANSFORMER_PROD>PROD" = { role = "TRANSFORMER_PROD", env = "PROD" }
    "TRANSFORMER_DEV>PROD"  = { role = "TRANSFORMER_DEV", env = "PROD" } # read, to clone
    "READER>PROD"           = { role = "READER", env = "PROD" }
  }
  account_role_name = local.all_roles[each.value.role]
  privileges        = ["USAGE"]
  on_account_object {
    object_type = "DATABASE"
    object_name = snowflake_database.env[each.value.env].name
  }
}

# Each transformer builds in its own environment's schemas.
resource "snowflake_grant_privileges_to_account_role" "transformer_schemas" {
  for_each          = local.env_schemas
  account_role_name = snowflake_account_role.transformer[each.value.env].name
  privileges        = ["USAGE", "CREATE TABLE", "CREATE VIEW"]
  on_schema {
    schema_name = snowflake_schema.layer[each.key].fully_qualified_name
  }
}

# DEV can see into PROD...
resource "snowflake_grant_privileges_to_account_role" "dev_reads_prod_schemas" {
  for_each          = local.prod_schemas
  account_role_name = snowflake_account_role.transformer["DEV"].name
  privileges        = ["USAGE"]
  on_schema {
    schema_name = snowflake_schema.layer[each.key].fully_qualified_name
  }
}

# ...and SELECT every table PROD's dbt run creates, which is all a zero-copy
# clone needs. Future grants, so Terraform never has to know the tables exist.
resource "snowflake_grant_privileges_to_account_role" "dev_reads_prod_future" {
  for_each          = { for p in setproduct(keys(local.prod_schemas), ["TABLES", "VIEWS"]) : "${p[0]}.${p[1]}" => { schema = p[0], kind = p[1] } }
  account_role_name = snowflake_account_role.transformer["DEV"].name
  privileges        = ["SELECT"]
  on_schema_object {
    future {
      object_type_plural = each.value.kind
      in_schema          = snowflake_schema.layer[each.value.schema].fully_qualified_name
    }
  }
}

resource "snowflake_grant_privileges_to_account_role" "reader_published" {
  account_role_name = snowflake_account_role.reader.name
  privileges        = ["USAGE"]
  on_schema {
    schema_name = snowflake_schema.layer["PROD.PUBLISHED"].fully_qualified_name
  }
}

# A table dbt creates in PROD.PUBLISHED tomorrow is readable by Streamlit
# without Terraform ever knowing it exists.
resource "snowflake_grant_privileges_to_account_role" "reader_future" {
  for_each          = toset(["TABLES", "VIEWS"])
  account_role_name = snowflake_account_role.reader.name
  privileges        = ["SELECT"]
  on_schema_object {
    future {
      object_type_plural = each.value
      in_schema          = snowflake_schema.layer["PROD.PUBLISHED"].fully_qualified_name
    }
  }
}
