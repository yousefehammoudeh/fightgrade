-- One row per judge's scorecard on a fight page: the whole-fight total only.
-- Scores are in the page's fighter order (fighter_1, fighter_2 in cln_ufcstats_fights).
with fights as (
    select payload as p
    from {{ ref('lnd_ufcstats_fights') }}
    {{ latest_fetch('payload:ufcstats_fight_id') }}
)
select
    f.p:ufcstats_fight_id::varchar      as ufcstats_fight_id,
    j.index + 1                         as judge_slot,
    trim(j.value:judge::varchar)        as judge_name,
    j.value:scores[0]::int              as score_1,
    j.value:scores[1]::int              as score_2
from fights f,
    lateral flatten(input => f.p:judges) j
