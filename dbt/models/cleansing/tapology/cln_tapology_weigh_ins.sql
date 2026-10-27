-- Official weigh-ins as the event page lists them, one row per pairing. Names
-- are abbreviated ('D. Eighmy'); matching them to bouts is REFINED's job.
with events as (
    select payload as p
    from {{ ref('lnd_tapology_event_details') }}
    {{ latest_fetch('payload:event_slug') }}
)
select
    e.p:event_slug::varchar                     as event_slug,
    w.index + 1                                 as weigh_in_slot,
    w.value:order::int                          as card_order,
    trim(w.value:left_name::varchar)            as left_name,
    {{ plausible_lbs("try_to_decimal(w.value:left_weight::varchar, 6, 2)") }} as left_weight_lbs,
    trim(w.value:right_name::varchar)           as right_name,
    {{ plausible_lbs("try_to_decimal(w.value:right_weight::varchar, 6, 2)") }} as right_weight_lbs
from events e,
    lateral flatten(input => e.p:weigh_ins) w
