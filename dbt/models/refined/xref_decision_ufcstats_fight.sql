-- Which UFC Stats fight each MMADecisions decision is: the bridge between the
-- judges' round scores (MMADecisions) and the round statistics (UFC Stats).
-- One to one. A decision is matched only when exactly one candidate is its
-- strongest; a fight two decisions both pick goes to the stronger match, and to
-- neither if they tie. Overrides (the seed) win over the computed match.
with candidates as (
    select * from {{ ref('stg_decision_fight_candidates') }}
),
best as (
    select * from candidates
    qualify rank() over (partition by decision_id order by tier, days_apart) = 1
),
decided as (
    select * from best
    qualify count(*) over (partition by decision_id) = 1
),
claimed as (
    select * from decided
    qualify rank() over (partition by ufcstats_fight_id order by tier, days_apart) = 1
        and count(*) over (partition by ufcstats_fight_id, tier, days_apart) = 1
),
overrides as (
    select * from {{ ref('xref_decision_ufcstats_fight_overrides') }}
),
matched as (
    select
        decision_id,
        ufcstats_fight_id,
        ufcstats_fighter_id_1,
        ufcstats_fighter_id_2,
        decode(tier, 1, 'names', 2, 'surnames', 3, 'one_name_and_a_shared_name', 4, 'one_name') as match_method,
        days_apart
    from claimed c
    where not exists (select 1 from overrides o where o.decision_id = c.decision_id)
      and not exists (select 1 from overrides o where o.ufcstats_fight_id = c.ufcstats_fight_id)
    union all
    -- an override attaches only to a decision and a fight we hold
    select
        o.decision_id,
        o.ufcstats_fight_id,
        o.ufcstats_fighter_id_1,
        iff(o.ufcstats_fighter_id_1 = f.fighter_1_id, f.fighter_2_id, f.fighter_1_id),
        'override',
        null
    from overrides o
    join {{ ref('cln_ufcstats_fights') }} f using (ufcstats_fight_id)
    join {{ ref('cln_mmadecisions_decisions') }} d using (decision_id)
),
decision_cards as (
    -- the judges' totals as MMADecisions lists them, fighter 1 first
    select
        decision_id,
        array_to_string(array_sort(array_agg(total_1 || '-' || total_2)), ',') as cards,
        count_if(totals_kind = 'legend') as legend_cards
    from {{ ref('cln_mmadecisions_judge_totals') }}
    group by 1
),
fight_cards as (
    -- UFC Stats' judges' totals, turned to the decision's fighter order
    select
        m.decision_id,
        array_to_string(array_sort(array_agg(
            iff(m.ufcstats_fighter_id_1 = f.fighter_1_id, j.score_1 || '-' || j.score_2, j.score_2 || '-' || j.score_1))), ',')
            as cards
    from matched m
    join {{ ref('cln_ufcstats_fights') }} f using (ufcstats_fight_id)
    join {{ ref('cln_ufcstats_fight_judges') }} j using (ufcstats_fight_id)
    group by 1
)
select
    m.decision_id,
    m.ufcstats_fight_id,
    -- the UFC Stats fighter who is the decision's fighter 1 (its winner, or first listed
    -- for a draw), and fighter 2: score pairs line up through these
    m.ufcstats_fighter_id_1,
    m.ufcstats_fighter_id_2,
    m.match_method,
    m.days_apart,
    -- the judges' totals agree on both sites; NULL where either has none to compare,
    -- or MMADecisions lists legend codes instead of points
    iff(dc.legend_cards > 0 or dc.cards is null or fc.cards is null, null, dc.cards = fc.cards)
        as scorecards_agree
from matched m
left join decision_cards dc using (decision_id)
left join fight_cards fc using (decision_id)
