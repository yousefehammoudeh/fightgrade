-- One row per decision. Fighters are in page order: the winner first, or as
-- listed for a draw. Every score pair downstream is (fighter 1, fighter 2).
with decisions as (
    select payload as p, _partition_date, _raw_html_key
    from {{ ref('lnd_mmadecisions_decisions') }}
    {{ latest_fetch('payload:decision_id') }}
),
fan_votes as (
    -- {"Faumui": 3, "Avila": 1, "Draw": 0}: who fans had winning, by surname
    select
        d.p:decision_id::varchar as decision_id,
        sum(iff({{ mmadecisions_side('f.key', 'd.p:fighter_1:name::varchar', 'd.p:fighter_2:name::varchar') }} = 1, f.value::int, 0)) as fan_votes_1,
        sum(iff({{ mmadecisions_side('f.key', 'd.p:fighter_1:name::varchar', 'd.p:fighter_2:name::varchar') }} = 2, f.value::int, 0)) as fan_votes_2,
        sum(iff({{ mmadecisions_is_draw('f.key') }}, f.value::int, 0)) as fan_votes_draw
    from decisions d,
        lateral flatten(input => d.p:fan_winner_counts) f
    group by 1
)
select
    p:decision_id::varchar                      as decision_id,
    p:event_id::varchar                         as mmadecisions_event_id,
    p:event_date::date                          as event_date,
    trim(p:event_name::varchar)                 as event_name,
    p:fighter_1:fighter_id::varchar             as fighter_1_id,
    trim(p:fighter_1:name::varchar)             as fighter_1_name,
    p:fighter_2:fighter_id::varchar             as fighter_2_id,
    trim(p:fighter_2:name::varchar)             as fighter_2_name,
    p:result::varchar = 'drew with'             as is_draw,
    -- 'Unanimous Decision', 'Split Decision', 'Majority Draw', ...
    p:method::varchar                           as method,
    {{ blank_to_null('p:referee::varchar') }}   as referee,
    {{ blank_to_null('p:location::varchar') }}  as location,
    p:promotion_logo::varchar                   as promotion_logo,
    p:n_judges::int                             as n_judges,
    p:n_media::int                              as n_media,
    -- tale of the tape, as the page shows it on fight night
    -- a few pages put a year (2005) or a typo (126) in the age slot: those are NULL
    iff(try_to_number(p:tale_of_the_tape:age[0]::varchar) between 15 and 60,
        try_to_number(p:tale_of_the_tape:age[0]::varchar), null) as age_1,
    iff(try_to_number(p:tale_of_the_tape:age[1]::varchar) between 15 and 60,
        try_to_number(p:tale_of_the_tape:age[1]::varchar), null) as age_2,
    {{ feet_inches('p:tale_of_the_tape:height[0]::varchar') }} as height_in_1,
    {{ feet_inches('p:tale_of_the_tape:height[1]::varchar') }} as height_in_2,
    {{ first_number('p:tale_of_the_tape:reach[0]::varchar') }} as reach_in_1,
    {{ first_number('p:tale_of_the_tape:reach[1]::varchar') }} as reach_in_2,
    {{ plausible_lbs(first_number('p:tale_of_the_tape:weight[0]::varchar')) }} as weight_lbs_1,
    {{ plausible_lbs(first_number('p:tale_of_the_tape:weight[1]::varchar')) }} as weight_lbs_2,
    p:fan_submitted::int                        as fan_scorecards,
    v.fan_votes_1,
    v.fan_votes_2,
    v.fan_votes_draw,
    _partition_date                             as fetch_date,
    _raw_html_key                               as raw_html_key
from decisions d
left join fan_votes v on v.decision_id = d.p:decision_id::varchar
