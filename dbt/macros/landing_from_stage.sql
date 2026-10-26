{#
  Read one entity's Parquet files straight from the external stage over
  gs://fightgrade-lake/parsed/. Each row keeps the whole record as a VARIANT
  plus audit columns; typing and filtering happen in CLEANSING and REFINED.

  Incremental runs skip every file already loaded, so a re-run only appends
  the new fetch_date partitions and never duplicates the old ones.
#}
{% macro landing_from_stage(entity) %}
select
    $1                                  as payload,
    metadata$filename                   as _object_key,
    to_date(regexp_substr(metadata$filename, 'fetch_date=([0-9-]+)', 1, 1, 'e'))
                                        as _partition_date,
    $1:_raw_html_key::varchar           as _raw_html_key,
    metadata$file_last_modified         as _file_last_modified,
    current_timestamp()                 as _loaded_at,
    '{{ invocation_id }}'               as _batch_id
from @{{ target.database }}.landing.lake_parsed/entity={{ entity }}/
    (file_format => '{{ target.database }}.landing.parquet_format')
{% if is_incremental() %}
where metadata$filename not in (select distinct _object_key from {{ this }})
{% endif %}
{% endmacro %}
