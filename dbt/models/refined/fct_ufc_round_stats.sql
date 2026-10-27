-- One row per fighter per round of every UFC Stats fight: the judge model's
-- features. The counts are cln_ufcstats_fight_rounds'; this adds the fight's
-- context, the opponent, how long the round ran and whether control time was
-- tracked. UFC Stats prints '--' for control on fights it did not track, never
-- for zero (that is 0:00), and always for the whole fight, so ctrl_tracked is
-- per fight and a NULL ctrl_seconds is never 0.
with fights as (
    select
        f.ufcstats_fight_id,
        f.ufcstats_event_id,
        e.event_date,
        f.fighter_1_id,
        f.fighter_2_id,
        f.gender,
        f.weight_class,
        f.is_title_bout,
        f.scheduled_rounds,
        f.round_minutes,
        f.finish_round,
        f.finish_round_seconds,
        f.method
    from {{ ref('cln_ufcstats_fights') }} f
    join {{ ref('cln_ufcstats_events') }} e using (ufcstats_event_id)
),
rounds as (
    select * from {{ ref('cln_ufcstats_fight_rounds') }}
),
tracked as (
    select ufcstats_fight_id, count_if(ctrl_seconds is null) = 0 as ctrl_tracked
    from rounds
    group by 1
)
select
    r.ufcstats_fight_id,
    f.ufcstats_event_id,
    f.event_date,
    r.round,
    r.ufcstats_fighter_id,
    iff(r.ufcstats_fighter_id = f.fighter_1_id, f.fighter_2_id, f.fighter_1_id) as opponent_fighter_id,
    f.gender,
    f.weight_class,
    f.is_title_bout,
    f.scheduled_rounds,
    f.method,
    -- the scheduled length, or less for the round a finish ended
    iff(r.round = f.finish_round, f.finish_round_seconds, f.round_minutes[r.round - 1]::int * 60)
                                                as round_seconds,
    r.knockdowns,
    r.sig_str_landed,
    r.sig_str_attempted,
    r.total_str_landed - r.sig_str_landed      as non_sig_str_landed,
    r.total_str_landed,
    r.total_str_attempted,
    r.head_landed,
    r.head_attempted,
    r.body_landed,
    r.body_attempted,
    r.leg_landed,
    r.leg_attempted,
    r.distance_landed,
    r.distance_attempted,
    r.clinch_landed,
    r.clinch_attempted,
    r.ground_landed,
    r.ground_attempted,
    r.td_landed,
    r.td_attempted,
    r.sub_attempts,
    r.reversals,
    r.ctrl_seconds,
    t.ctrl_tracked
from rounds r
join fights f using (ufcstats_fight_id)
join tracked t using (ufcstats_fight_id)
