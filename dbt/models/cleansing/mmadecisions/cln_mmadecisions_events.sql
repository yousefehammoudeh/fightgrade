select
    payload:event_id::varchar                   as mmadecisions_event_id,
    trim(payload:event_name::varchar)           as event_name,
    payload:event_date::date                    as event_date,
    {{ blank_to_null('payload:location::varchar') }} as location,
    trim(regexp_substr(payload:location::varchar, '[^,]+$')) as country,
    {{ blank_to_null('payload:venue::varchar') }} as venue,
    -- the promotion as the page's logo names it: ufc, bellator, pfl, cw, ... ('box' is boxing)
    payload:promotion_logo::varchar             as promotion_logo,
    array_size(payload:decisions)               as n_decisions,
    _partition_date                             as fetch_date,
    _raw_html_key                               as raw_html_key
from {{ ref('lnd_mmadecisions_events') }}
{{ latest_fetch('payload:event_id') }}
