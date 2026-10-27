-- One row per judge per round: the judge model's target. Scores are
-- (fighter 1, fighter 2); NULL where the site shows "-" (round not known).
select
    payload:decision_id::varchar                as decision_id,
    payload:judge_slot::int                     as judge_slot,
    payload:judge_id::varchar                   as judge_id,
    iff(payload:judge_name::varchar = 'Unknown Judge', null, trim(payload:judge_name::varchar))
                                                as judge_name,
    payload:round::int                          as round,
    payload:score_1::int                        as score_1,
    payload:score_2::int                        as score_2
from {{ ref('lnd_mmadecisions_judge_rounds') }}
{{ latest_fetch('payload:decision_id') }}
