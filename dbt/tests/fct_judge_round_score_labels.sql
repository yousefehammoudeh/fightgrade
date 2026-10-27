-- Every row the model may train on has a whole label: the seven scores' shares
-- sum to 1, a judge's margin is set, and fighter A comes before B by id, never
-- by the decision's order (which puts the winner first).
select ufcstats_fight_id, round, rater_key, margin, fighter_a_id, fighter_b_id,
       p_7_10 + p_8_10 + p_9_10 + p_10_10 + p_10_9 + p_10_8 + p_10_7 as label_sum
from {{ ref('fct_judge_round_score') }}
where exclusion_reason is null
  and (abs(p_7_10 + p_8_10 + p_9_10 + p_10_10 + p_10_9 + p_10_8 + p_10_7 - 1) > 1e-6
       or (rater_kind = 'judge' and margin is null)
       or fighter_a_id >= fighter_b_id)
