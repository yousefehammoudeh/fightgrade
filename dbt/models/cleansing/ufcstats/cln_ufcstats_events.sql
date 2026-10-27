select
    payload:ufcstats_event_id::varchar          as ufcstats_event_id,
    trim(payload:event_name::varchar)           as event_name,
    payload:event_date::date                    as event_date,
    {{ blank_to_null('payload:location::varchar') }} as location,
    trim(regexp_substr(payload:location::varchar, '[^,]+$'))
                                                as country,
    array_size(payload:fights)                  as n_fights,
    _partition_date                             as fetch_date,
    _raw_html_key                               as raw_html_key
from {{ ref('lnd_ufcstats_events') }}
{{ latest_fetch('payload:ufcstats_event_id') }}
