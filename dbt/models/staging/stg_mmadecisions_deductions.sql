-- The deductions each decision carried: the page's, unless the override seed
-- has rows for that decision, which then replace the page's entirely.
with overrides as (
    select * from {{ ref('mmadecisions_deduction_overrides') }}
)
select decision_id, round, points, fighter_side, 'page' as deduction_source
from {{ ref('cln_mmadecisions_deductions') }} d
where not exists (select 1 from overrides o where o.decision_id = d.decision_id)
union all
select decision_id, round, points, fighter_side, 'override'
from overrides
