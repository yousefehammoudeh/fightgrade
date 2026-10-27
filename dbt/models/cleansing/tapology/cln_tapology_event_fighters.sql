-- One row per fighter per bout, including bouts cancelled or fizzled before
-- fight night (result 'cancelled'/'fizzled', usually no side).
select
    payload:bout_slug::varchar                  as bout_slug,
    payload:bout_id::int                        as bout_id,
    payload:event_slug::varchar                 as event_slug,
    payload:fighter_slug::varchar               as fighter_slug,
    trim(payload:name::varchar)                 as name,
    payload:side::int                           as side,
    payload:result::varchar                     as result,
    payload:sport::varchar                      as sport,
    payload:title_kind::varchar                 as title_kind
from {{ ref('lnd_tapology_event_fighters') }}
{{ latest_fetch('payload:event_slug') }}
