-- One row per judge per decision. totals_kind 'legend' marks early cards whose
-- TOTAL is a code (0-0 unknown, 1-0 favoured, 1-1 draw), not points.
select
    payload:decision_id::varchar                as decision_id,
    payload:judge_slot::int                     as judge_slot,
    payload:judge_id::varchar                   as judge_id,
    iff(payload:judge_name::varchar = 'Unknown Judge', null, trim(payload:judge_name::varchar))
                                                as judge_name,
    payload:total_1::int                        as total_1,
    payload:total_2::int                        as total_2,
    payload:totals_kind::varchar                as totals_kind,
    payload:rounds_listed::int                  as rounds_listed,
    payload:rounds_known::int                   as rounds_known
from {{ ref('lnd_mmadecisions_judge_totals') }}
{{ latest_fetch('payload:decision_id') }}
