-- The referee of each bout, from the event page's referee list.
with events as (
    select payload as p
    from {{ ref('lnd_tapology_event_details') }}
    {{ latest_fetch('payload:event_slug') }}
)
select
    e.p:event_slug::varchar                     as event_slug,
    r.value:bout_slug::varchar                  as bout_slug,
    r.value:bout_id::int                        as bout_id,
    {{ blank_to_null('r.value:referee::varchar') }} as referee
from events e,
    lateral flatten(input => e.p:referees) r
where r.value:bout_slug is not null
