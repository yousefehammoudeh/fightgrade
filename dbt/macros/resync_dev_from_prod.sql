{#
  Reset DEV to PROD: every PROD table is zero-copy cloned over its DEV
  counterpart, and DEV tables PROD doesn't have are dropped. A clone shares
  PROD's storage until either side changes, so a full resync costs seconds and
  no extra storage.

      dbt run-operation resync_dev_from_prod
      dbt run-operation resync_dev_from_prod --args '{drop_dev_only: false}'

  Pass drop_dev_only: false to keep DEV-only tables (work in progress you
  haven't merged yet). Views can't be cloned; the next `dbt build` in DEV
  recreates them against DEV.

  It only ever runs as the dev target, and TRANSFORMER_DEV can read PROD but
  not write it — so a resync can't damage PROD even if pointed the wrong way.
#}
{% macro resync_dev_from_prod(drop_dev_only=true) %}
    {% if target.name != 'dev' %}
        {{ exceptions.raise_compiler_error(
            "resync_dev_from_prod overwrites " ~ target.database ~ "; run it with --target dev") }}
    {% endif %}
    {% if not execute %}{{ return(none) }}{% endif %}

    {% set prod = var('prod_database') %}
    {% set dev = target.database %}
    {% set cloned = [] %}
    {% set dropped = [] %}

    {% for layer in var('layers') %}
        {# dbt builds transient tables by default, and a transient table can
           only be cloned into another transient one. SHOW TERSE TABLES reports
           both as TABLE, so read IS_TRANSIENT from INFORMATION_SCHEMA. #}
        {% set shown = run_query("select table_name, is_transient from " ~ prod ~ ".information_schema.tables"
                                 ~ " where table_schema = '" ~ layer ~ "' and table_type = 'BASE TABLE'") %}
        {% set prod_tables = shown.columns['TABLE_NAME'].values() %}
        {% for row in shown.rows %}
            {% set kind = 'transient table' if row['IS_TRANSIENT'] == 'YES' else 'table' %}
            {% do run_query('create or replace ' ~ kind ~ ' ' ~ dev ~ '.' ~ layer ~ '."' ~ row['TABLE_NAME'] ~ '" clone '
                            ~ prod ~ '.' ~ layer ~ '."' ~ row['TABLE_NAME'] ~ '"') %}
            {% do cloned.append(layer ~ '.' ~ row['TABLE_NAME']) %}
        {% endfor %}

        {% if drop_dev_only %}
            {% set dev_tables = run_query("show terse tables in schema " ~ dev ~ "." ~ layer).columns['name'].values() %}
            {% for t in dev_tables if t not in prod_tables %}
                {% do run_query('drop table ' ~ dev ~ '.' ~ layer ~ '."' ~ t ~ '"') %}
                {% do dropped.append(layer ~ '.' ~ t) %}
            {% endfor %}
        {% endif %}
    {% endfor %}

    {{ log("cloned " ~ cloned | length ~ " tables from " ~ prod ~ " into " ~ dev ~ ": " ~ cloned | join(', '), info=true) }}
    {% if drop_dev_only %}
        {{ log("dropped " ~ dropped | length ~ " DEV-only tables: " ~ dropped | join(', '), info=true) }}
    {% endif %}
{% endmacro %}
