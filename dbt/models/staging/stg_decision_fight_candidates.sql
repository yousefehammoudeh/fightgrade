-- Every UFC Stats fight a MMADecisions decision could be: within a day of it (the
-- sources disagree on dates across midnight and time zones) and with at least one
-- fighter's name matching. Each pairing is scored in both fighter orders; the
-- better order is kept, and a candidate whose two orders tie is dropped.
with decisions as (
    select
        decision_id,
        event_date,
        {{ name_key('fighter_1_name') }} as d1,
        {{ name_key('fighter_2_name') }} as d2
    from {{ ref('cln_mmadecisions_decisions') }}
),
fights as (
    select
        f.ufcstats_fight_id,
        e.event_date,
        f.fighter_1_id,
        f.fighter_2_id,
        {{ name_key('a.name') }} as u1,
        {{ name_key('b.name') }} as u2
    from {{ ref('cln_ufcstats_fights') }} f
    join {{ ref('cln_ufcstats_events') }} e using (ufcstats_event_id)
    join {{ ref('cln_ufcstats_fighters') }} a on a.ufcstats_fighter_id = f.fighter_1_id
    join {{ ref('cln_ufcstats_fighters') }} b on b.ufcstats_fighter_id = f.fighter_2_id
),
scored as (
    select
        d.decision_id,
        f.ufcstats_fight_id,
        abs(datediff(day, d.event_date, f.event_date)) as days_apart,
        f.fighter_1_id,
        f.fighter_2_id,
        {{ pairing_tier(name_similarity('d.d1', 'f.u1'), name_similarity('d.d2', 'f.u2')) }} as tier_same,
        {{ pairing_tier(name_similarity('d.d1', 'f.u2'), name_similarity('d.d2', 'f.u1')) }} as tier_swapped
    from decisions d
    join fights f
      on f.event_date between dateadd(day, -1, d.event_date) and dateadd(day, 1, d.event_date)
)
select
    decision_id,
    ufcstats_fight_id,
    days_apart,
    least(coalesce(tier_same, 9), coalesce(tier_swapped, 9)) as tier,
    -- the UFC Stats fighters in the decision's order (fighter 1 is its winner, or first listed for a draw)
    iff(coalesce(tier_same, 9) < coalesce(tier_swapped, 9), fighter_1_id, fighter_2_id) as ufcstats_fighter_id_1,
    iff(coalesce(tier_same, 9) < coalesce(tier_swapped, 9), fighter_2_id, fighter_1_id) as ufcstats_fighter_id_2
from scored
where coalesce(tier_same, tier_swapped) is not null
  and coalesce(tier_same, 9) <> coalesce(tier_swapped, 9)
