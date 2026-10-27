-- One row per judge's scorecard on a fight page: the whole-fight total only,
-- in the fight page's fighter order (fighter_1, fighter_2 in cln_ufcstats_fights).
-- UFC Stats prints each judge's scores in the reverse of the event page's fighter
-- order (the event page lists the winner first), whichever way the fight page
-- lists them; checked against MMADecisions' cards on 4,300 matched decisions.
with fights as (
    select payload as p
    from {{ ref('lnd_ufcstats_fights') }}
    {{ latest_fetch('payload:ufcstats_fight_id') }}
),
card as (
    select ufcstats_fight_id, fighter_1_id as event_page_fighter_1_id
    from {{ ref('cln_ufcstats_event_fights') }}
)
select
    f.p:ufcstats_fight_id::varchar      as ufcstats_fight_id,
    j.index + 1                         as judge_slot,
    trim(j.value:judge::varchar)        as judge_name,
    -- the printed pair is (event page's fighter 2, event page's fighter 1)
    iff(c.event_page_fighter_1_id = f.p:red_fighter_id::varchar, j.value:scores[1], j.value:scores[0])::int
                                        as score_1,
    iff(c.event_page_fighter_1_id = f.p:red_fighter_id::varchar, j.value:scores[0], j.value:scores[1])::int
                                        as score_2
from fights f
join card c on c.ufcstats_fight_id = f.p:ufcstats_fight_id::varchar,
    lateral flatten(input => f.p:judges) j
