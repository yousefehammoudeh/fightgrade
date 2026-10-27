-- The card as the event page lists it, main event first: where a fight sat on
-- the card, which the fight page does not say.
with events as (
    select payload as p
    from {{ ref('lnd_ufcstats_events') }}
    {{ latest_fetch('payload:ufcstats_event_id') }}
)
select
    e.p:ufcstats_event_id::varchar              as ufcstats_event_id,
    f.value:ufcstats_fight_id::varchar          as ufcstats_fight_id,
    f.index + 1                                 as card_position,
    f.value:fighter_ids[0]::varchar             as fighter_1_id,
    f.value:fighter_ids[1]::varchar             as fighter_2_id,
    f.value:result_flags                        as result_flags,
    f.value:bonuses                             as bonuses
from events e,
    lateral flatten(input => e.p:fights) f
