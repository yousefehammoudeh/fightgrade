-- One row per decision: what the real panel, an average panel, the crowd and random
-- panels make of the fight's round statistics, beside what happened (DESIGN.md §8).
-- Fighter A and B are the fight's UFC Stats fighters ordered by id; the result says
-- who won. p_actual_result is how likely the result was: low is what the stats call
-- a robbery, which is not the same as one.
with cards as (
    select decision_id,
           listagg(rater_name || ' ' || iff(actual_card_margin > 0, '+', '') || actual_card_margin, ', ')
               within group (order by rater_key) as judges_cards
    from {{ source('judge_lens', 'judge_lens_cards') }}
    group by 1
)
select
    f.decision_id,
    f.ufcstats_fight_id,
    e.event_name,
    f.event_date,
    a.name                                          as fighter_a,
    b.name                                          as fighter_b,
    case f.result when 'A' then a.name when 'B' then b.name end as winner,
    f.result,
    f.rounds,
    c.judges_cards,
    f.deducted_a,
    f.deducted_b,
    f.p_a_wins,
    f.p_draw,
    f.p_b_wins,
    case f.result when 'A' then f.p_a_wins when 'B' then f.p_b_wins else f.p_draw end as p_actual_result,
    f.p_a_unanimous, f.p_a_split, f.p_a_majority, f.p_b_unanimous, f.p_b_split, f.p_b_majority,
    f.average_panel_p_a_wins,
    f.average_panel_p_b_wins,
    f.crowd_card_p_a,
    f.crowd_card_p_level,
    f.crowd_card_p_b,
    f.random_panels_p_a_median,
    f.random_panels_p_a_p10,
    f.random_panels_p_a_p90,
    f.random_panels_share_favouring_a,
    f.random_panels_judges,
    f.model_version
from {{ source('judge_lens', 'judge_lens_fights') }} f
join {{ ref('cln_ufcstats_events') }} e using (ufcstats_event_id)
left join {{ ref('cln_ufcstats_fighters') }} a on a.ufcstats_fighter_id = f.fighter_a_id
left join {{ ref('cln_ufcstats_fighters') }} b on b.ufcstats_fighter_id = f.fighter_b_id
left join cards c using (decision_id)
