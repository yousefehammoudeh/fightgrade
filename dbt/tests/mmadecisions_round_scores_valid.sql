-- Every round on a points card must be a valid score once its deductions are
-- added back: the winner on 10, the loser on 7-10. A failure is a deduction the
-- page put in the wrong round or left out (fix it in
-- seeds/mmadecisions_deduction_overrides.csv), a card that cannot be reconciled
-- (seeds/mmadecisions_round_score_quarantine.csv), or a legend code taken for points.
-- Only decisions matched to a UFC Stats fight, the judge model's: MMADecisions
-- also scores boxing, where a 10-6 is real.
with deductions as (
    select decision_id, round,
           sum(iff(fighter_side = 1, points, 0)) as d1,
           sum(iff(fighter_side = 2, points, 0)) as d2
    from {{ ref('stg_mmadecisions_deductions') }}
    group by 1, 2
),
rounds as (
    select r.decision_id, r.judge_slot, r.round, r.score_1, r.score_2,
           r.score_1 + coalesce(d.d1, 0) as s1,
           r.score_2 + coalesce(d.d2, 0) as s2
    from {{ ref('cln_mmadecisions_judge_rounds') }} r
    join {{ ref('cln_mmadecisions_judge_totals') }} t using (decision_id, judge_slot)
    join {{ ref('xref_decision_ufcstats_fight') }} x using (decision_id)
    left join deductions d using (decision_id, round)
    where t.totals_kind = 'score'
      and not exists (select 1 from {{ ref('mmadecisions_round_score_quarantine') }} q
                      where q.decision_id = r.decision_id)
)
select *
from rounds
where (score_1 is null) <> (score_2 is null)
   or (s1 is not null and (greatest(s1, s2) <> 10 or least(s1, s2) < 7))
