-- Bouts the event page lists as cancelled, with the reason where given.
with events as (
    select payload as p
    from {{ ref('lnd_tapology_event_details') }}
    {{ latest_fetch('payload:event_slug') }}
)
select
    e.p:event_slug::varchar                     as event_slug,
    x.value:bout_slug::varchar                  as bout_slug,
    x.value:bout_id::int                        as bout_id,
    x.value:status::varchar                     as status,
    {{ blank_to_null('x.value:reason::varchar') }} as reason,
    x.value:fighters[0]:fighter_slug::varchar   as fighter_1_slug,
    x.value:fighters[1]:fighter_slug::varchar   as fighter_2_slug
from events e,
    lateral flatten(input => e.p:cancelled_bouts) x
