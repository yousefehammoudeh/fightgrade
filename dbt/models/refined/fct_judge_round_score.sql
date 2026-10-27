-- One row per rater per round of every decision matched to a UFC Stats fight:
-- the judge model's target (DESIGN.md §8). A rater is a judge (a hard call) or
-- the crowd (the fans' round scores, as a distribution).
--
-- Fighter A and B are the fight's two UFC Stats fighters ordered by id, which
-- says nothing about the result. MMADecisions' own order puts the winner first,
-- and a model shown that order learns "fighter 1 wins".
--
-- The label is the round score before deductions: MMADecisions' round scores,
-- the judges' and the fans', include them (Long's -2 shows as 9-8 or 10-7), so
-- the deducted points are added back. p_7_10 .. p_10_7 are the label as a
-- distribution over the seven scores from A's side: one-hot for a judge, the
-- fans' shares for the crowd. margin is A's points less B's, for judges.
--
-- exclusion_reason is NULL for a row the model may train on, else why not.
-- period and cv_fold are the protocol's split, by event, fixed by a hash of
-- the event id so new fights never move old ones.
with decisions as (
    select
        x.decision_id,
        x.ufcstats_fight_id,
        f.ufcstats_event_id,
        e.event_date,
        d.promotion_logo,
        least(x.ufcstats_fighter_id_1, x.ufcstats_fighter_id_2)    as fighter_a_id,
        greatest(x.ufcstats_fighter_id_1, x.ufcstats_fighter_id_2) as fighter_b_id,
        -- whether A is the decision's fighter 1, to turn its pairs into A/B
        x.ufcstats_fighter_id_1 < x.ufcstats_fighter_id_2       as a_is_1
    from {{ ref('xref_decision_ufcstats_fight') }} x
    join {{ ref('cln_mmadecisions_decisions') }} d using (decision_id)
    join {{ ref('cln_ufcstats_fights') }} f using (ufcstats_fight_id)
    join {{ ref('cln_ufcstats_events') }} e using (ufcstats_event_id)
),
deductions as (
    select decision_id, round,
           sum(iff(fighter_side = 1, points, 0)) as ded_1,
           sum(iff(fighter_side = 2, points, 0)) as ded_2
    from {{ ref('stg_mmadecisions_deductions') }}
    group by 1, 2
),
stats as (
    -- the round has both fighters' stats, and control time was tracked
    select ufcstats_fight_id, round, count(*) = 2 as has_stats, booland_agg(ctrl_tracked) as ctrl_tracked
    from {{ ref('fct_ufc_round_stats') }}
    group by 1, 2
),
judges as (
    select
        d.*,
        r.round,
        'judge:' || r.judge_slot                    as rater_key,
        'judge'                                     as rater_kind,
        r.judge_id                                  as rater_id,
        r.judge_name                                as rater_name,
        t.totals_kind,
        r.score_1,
        r.score_2,
        coalesce(x.ded_1, 0)                        as ded_1,
        coalesce(x.ded_2, 0)                        as ded_2,
        r.score_1 + coalesce(x.ded_1, 0)            as raw_1,
        r.score_2 + coalesce(x.ded_2, 0)            as raw_2
    from decisions d
    join {{ ref('cln_mmadecisions_judge_rounds') }} r using (decision_id)
    join {{ ref('cln_mmadecisions_judge_totals') }} t using (decision_id, judge_slot)
    left join deductions x using (decision_id, round)
),
judge_rows as (
    select
        j.*,
        iff(a_is_1, raw_1, raw_2) - iff(a_is_1, raw_2, raw_1) as margin,
        case
            when totals_kind = 'legend' then 'legend_card'
            when exists (select 1 from {{ ref('mmadecisions_round_score_quarantine') }} q
                         where q.decision_id = j.decision_id) then 'quarantined'
            when score_1 is null or score_2 is null then 'no_score'
            when not {{ valid_round_score('raw_1', 'raw_2') }} then 'invalid_score'
        end as score_exclusion
    from judges j
),
fan_outcomes as (
    -- each outcome the fans gave a round, as (fighter 1, fighter 2) points.
    -- Fans enter the deductions too, so they are added back; an outcome that
    -- is only valid as entered is a fan who left the deduction out.
    select
        d.*,
        s.round,
        s.share,
        s.est_count,
        iff(s.winner_side = 2, s.score_lo, s.score_hi) as p1,
        iff(s.winner_side = 2, s.score_hi, s.score_lo) as p2,
        coalesce(x.ded_1, 0) as ded_1,
        coalesce(x.ded_2, 0) as ded_2
    from decisions d
    join {{ ref('cln_mmadecisions_fan_scores') }} s using (decision_id)
    left join deductions x using (decision_id, round)
    where s.scope = 'round'
      and (s.winner_side is not null or s.is_draw)     -- a surname both fighters share cannot be placed
),
fan_scored as (
    select
        o.*,
        case when {{ valid_round_score('p1 + ded_1', 'p2 + ded_2') }} then p1 + ded_1
             when {{ valid_round_score('p1', 'p2') }} then p1 end as raw_1,
        case when {{ valid_round_score('p1 + ded_1', 'p2 + ded_2') }} then p2 + ded_2
             when {{ valid_round_score('p1', 'p2') }} then p2 end as raw_2
    from fan_outcomes o
),
crowd_rows as (
    select
        decision_id, ufcstats_fight_id, ufcstats_event_id, event_date, promotion_logo,
        fighter_a_id, fighter_b_id, a_is_1, round,
        iff(a_is_1, raw_1, raw_2) - iff(a_is_1, raw_2, raw_1) as margin_a,
        share,
        est_count
    from fan_scored
    where raw_1 is not null
),
crowd as (
    select
        decision_id, ufcstats_fight_id, ufcstats_event_id, event_date, promotion_logo,
        fighter_a_id, fighter_b_id, round,
        sum(share)                                  as share_kept,
        sum(est_count)                              as n_fans,
        {% for m, name in [(-3, '7_10'), (-2, '8_10'), (-1, '9_10'), (0, '10_10'), (1, '10_9'), (2, '10_8'), (3, '10_7')] -%}
        sum(iff(margin_a = {{ m }}, share, 0)) / sum(share) as p_{{ name }}{{ ',' if not loop.last }}
        {% endfor %}
    from crowd_rows
    group by all
),
unioned as (
    select
        decision_id, ufcstats_fight_id, ufcstats_event_id, event_date, promotion_logo,
        fighter_a_id, fighter_b_id, round,
        rater_key, rater_kind, rater_id, rater_name,
        iff(a_is_1, score_1, score_2)               as score_a,
        iff(a_is_1, score_2, score_1)               as score_b,
        iff(a_is_1, ded_1, ded_2)                   as deducted_a,
        iff(a_is_1, ded_2, ded_1)                   as deducted_b,
        iff(score_exclusion is null, margin, null)  as margin,
        {% for m, name in [(-3, '7_10'), (-2, '8_10'), (-1, '9_10'), (0, '10_10'), (1, '10_9'), (2, '10_8'), (3, '10_7')] -%}
        iff(score_exclusion is null, iff(margin = {{ m }}, 1, 0), null) as p_{{ name }},
        {% endfor -%}
        1                                           as label_weight,
        null::number(38, 3)                         as share_kept,
        score_exclusion
    from judge_rows
    union all
    select
        decision_id, ufcstats_fight_id, ufcstats_event_id, event_date, promotion_logo,
        fighter_a_id, fighter_b_id, round,
        'crowd', 'crowd', 'crowd', 'Crowd',
        null, null, null, null, null,
        p_7_10, p_8_10, p_9_10, p_10_10, p_10_9, p_10_8, p_10_7,
        n_fans,
        share_kept,
        null
    from crowd
)
select
    u.ufcstats_fight_id,
    u.round,
    u.rater_key,
    u.rater_kind,
    u.rater_id,
    u.rater_name,
    u.decision_id,
    u.ufcstats_event_id,
    u.event_date,
    u.promotion_logo,
    u.fighter_a_id,
    u.fighter_b_id,
    u.score_a,
    u.score_b,
    u.deducted_a,
    u.deducted_b,
    u.margin,
    u.p_7_10, u.p_8_10, u.p_9_10, u.p_10_10, u.p_10_9, u.p_10_8, u.p_10_7,
    u.label_weight,
    u.share_kept,
    coalesce(u.score_exclusion,
             case when not coalesce(s.has_stats, false) then 'no_stats'
                  when not s.ctrl_tracked then 'no_control' end) as exclusion_reason,
    case when u.event_date < '2023-01-01' then 'select'
         when u.event_date < '2025-01-01' then 'forward'
         else 'test' end                            as period,
    mod(to_number(left(md5(u.ufcstats_event_id), 8), 'XXXXXXXX'), 5) as cv_fold
from unioned u
left join stats s using (ufcstats_fight_id, round)
