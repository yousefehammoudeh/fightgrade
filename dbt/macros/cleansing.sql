{#
  Helpers for CLEANSING: hygiene only (DESIGN.md §5) — typing, dedup, blanks to
  NULL. Scope (sport, level, status) is applied in REFINED, never here.
#}

{#
  Keep the newest fetch of each `partition_by`: every row from the latest file
  that holds it. A page fetched again (an event whose results came in late, a
  fighter refreshed for a new card) supersedes the old copy, and rows it no
  longer lists go with it. For a child table, partition by the parent page's key.
#}
{% macro latest_fetch(partition_by) -%}
qualify dense_rank() over (partition by {{ partition_by }}
                           order by _partition_date desc, _loaded_at desc, _object_key desc) = 1
{%- endmacro %}

{# Empty strings and the sources' placeholders for "nothing here" become NULL. #}
{% macro blank_to_null(expr) -%}
iff(trim({{ expr }}) in ('', '-', '--', 'N/A', 'n/a', 'NA', 'null'), null, trim({{ expr }}))
{%- endmacro %}

{# 'm:ss' (or 'mm:ss') to seconds; anything else is NULL. #}
{% macro clock_seconds(expr) -%}
iff(regexp_like({{ expr }}, '^[0-9]{1,2}:[0-9]{2}$'),
    split_part({{ expr }}, ':', 1)::int * 60 + split_part({{ expr }}, ':', 2)::int, null)
{%- endmacro %}

{# The first decimal number in a string ('155 lbs.', '71.5"', '62%'), or NULL. #}
{% macro first_number(expr) -%}
try_to_decimal(regexp_substr({{ expr }}, '[0-9]+(\\.[0-9]+)?'), 10, 2)
{%- endmacro %}

{# A body weight in pounds, or NULL outside 40-500: kilogram slips and typos. #}
{% macro plausible_lbs(expr) -%}
iff({{ expr }} between 40 and 500, {{ expr }}, null)
{%- endmacro %}

{# Feet and inches ('5\'10"', '6\'2" (188cm)') to inches, or NULL. #}
{% macro feet_inches(expr) -%}
try_to_number(regexp_substr({{ expr }}, '([0-9])\'', 1, 1, 'e', 1)) * 12
    + coalesce(try_to_decimal(regexp_substr({{ expr }}, '\'\\s*([0-9]+(\\.[0-9]+)?)"', 1, 1, 'e', 1), 10, 2), 0)
{%- endmacro %}
