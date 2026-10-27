with fights as (
    select payload as p, _partition_date, _raw_html_key
    from {{ ref('lnd_ufcstats_fights') }}
    {{ latest_fetch('payload:ufcstats_fight_id') }}
),
typed as (
    select
        p,
        _partition_date,
        _raw_html_key,
        p:round::int as finish_round,
        {{ clock_seconds('p:time::varchar') }} as finish_round_seconds,
        -- '3 Rnd (5-5-5)' -> [5, 5, 5]; '1 Rnd + OT (12-3)' -> [12, 3]; 'No Time Limit' -> NULL
        transform(split(regexp_substr(p:time_format::varchar, '\\(([0-9-]+)\\)', 1, 1, 'e', 1), '-'),
                  m varchar -> m::int) as round_minutes
    from fights
)
select
    p:ufcstats_fight_id::varchar                as ufcstats_fight_id,
    p:ufcstats_event_id::varchar                as ufcstats_event_id,
    -- the fight page's first and second fighter (the export's red/blue), not corners
    p:red_fighter_id::varchar                   as fighter_1_id,
    p:blue_fighter_id::varchar                  as fighter_2_id,
    p:results[0]::varchar                       as result_1,
    p:results[1]::varchar                       as result_2,
    iff(p:winner_fighter_id::varchar in ('draw', 'nc', ''), null, p:winner_fighter_id::varchar)
                                                as winner_fighter_id,
    p:gender::varchar                           as gender,
    p:weight_class::varchar                     as weight_class,
    p:is_title_bout::boolean                    as is_title_bout,
    p:method::varchar                           as method,
    {{ blank_to_null('p:method_detail::varchar') }} as method_detail,
    {{ blank_to_null('p:referee::varchar') }}   as referee,
    finish_round,
    finish_round_seconds,
    -- earlier rounds at their scheduled length, plus the clock in the last one
    case
        when p:time_format::varchar like 'Unlimited Rnd%'   -- every round the one listed length
            then (finish_round - 1) * round_minutes[0]::int * 60 + finish_round_seconds
        when array_size(round_minutes) >= finish_round
            then reduce(array_slice(round_minutes, 0, finish_round - 1), 0, (acc, m) -> acc + m) * 60
                 + finish_round_seconds
    end                                         as fight_seconds,
    p:time_format::varchar                      as time_format,
    round_minutes,
    p:scheduled_rounds::int                     as scheduled_rounds,
    p:bonuses                                   as bonuses,
    array_size(p:judges)                        as n_judges,
    _partition_date                             as fetch_date,
    _raw_html_key                               as raw_html_key
from typed
