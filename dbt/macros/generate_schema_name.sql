{#
  Use the configured schema verbatim. dbt's default would prefix it with the
  target schema (STAGING_LANDING), inventing schemas Terraform doesn't own —
  and Terraform owns every schema here (DESIGN.md §5).
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ (custom_schema_name or target.schema) | trim }}
{%- endmacro %}
