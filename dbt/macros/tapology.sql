{#
  Tapology's LANDING holds two layouts (landing_from_stage): the 2026-08 bulk
  load, typed Parquet columns, and JSON rows from the crawls since. Where they
  differ, these read both the same way.
#}

{# A {sport: count} map: the bulk load stored it as {"key_value": [{key, value}]}. #}
{% macro tapology_counts(expr) -%}
iff({{ expr }}:key_value is not null,
    reduce({{ expr }}:key_value, object_construct(), (acc, kv) -> object_insert(acc, kv:key::varchar, kv:value)),
    {{ expr }})
{%- endmacro %}

{# The archived page: an audit column for crawled rows, inside the payload for bulk ones. #}
{% macro tapology_raw_html_key() -%}
coalesce(_raw_html_key, payload:_raw_html_key::varchar)
{%- endmacro %}

{# '6\'2" (188cm)', '77.0" (196cm)': the centimetres Tapology gives in brackets. #}
{% macro tapology_cm(expr) -%}
try_to_number(regexp_substr({{ expr }}, '\\(([0-9]+)\\s*cm\\)', 1, 1, 'e', 1))
{%- endmacro %}

{# '38 years, 5 months, 2 weeks, 2 days': one component, 0 when the age omits it, NULL with no age. #}
{% macro tapology_age_part(expr, unit) -%}
iff({{ expr }} is null, null, coalesce(try_to_number(regexp_substr({{ expr }}, '([0-9]+) {{ unit }}', 1, 1, 'e', 1)), 0))
{%- endmacro %}
