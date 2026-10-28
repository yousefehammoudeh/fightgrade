-- One row per round and rater of every decision: each judge, the crowd (the fans'
-- scores as a distribution) and the average judge (rater_key 'average'). The seven
-- scores are fighter A's; p_call is how likely the call actually made was for that
-- judge, p_call_average for the average judge. The average judge's rows carry what
-- each group of statistics added to its score for A (contrib_*, log-odds).
select
    r.*,
    a.name as fighter_a,
    b.name as fighter_b
from {{ source('judge_lens', 'judge_lens_rounds') }} r
left join {{ ref('cln_ufcstats_fighters') }} a on a.ufcstats_fighter_id = r.fighter_a_id
left join {{ ref('cln_ufcstats_fighters') }} b on b.ufcstats_fighter_id = r.fighter_b_id
