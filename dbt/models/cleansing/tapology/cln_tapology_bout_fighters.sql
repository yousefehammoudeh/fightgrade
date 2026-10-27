-- One row per fighter per bout on a card, with the bout page's tale of the tape
-- (Tapology's "comparison": left is side 0, right side 1). What the fighter's
-- profile would say is rendered into every bout, so no fighter pages are needed.
with bouts as (
    select payload as p
    from {{ ref('lnd_tapology_bouts') }}
    {{ latest_fetch('payload:event_slug') }}
),
fighters as (
    select
        b.p:bout_slug::varchar                  as bout_slug,
        b.p:event_slug::varchar                 as event_slug,
        f.value                                 as f,
        iff(f.value:side::int = 0, 'left', 'right') as lr,
        b.p:comparison                          as c
    from bouts b,
        lateral flatten(input => b.p:fighters) f
),
aged as (
    -- an age over 80 is a typo on the page (one says 119): no age at all
    select *,
        iff({{ tapology_age_part('get(c:age_at_fight, lr)::varchar', 'year') }} <= 80,
            get(c:age_at_fight, lr)::varchar, null) as age_text
    from fighters
)
select
    bout_slug,
    event_slug,
    f:side::int                                 as side,
    f:fighter_slug::varchar                     as fighter_slug,
    -- Tapology lists some fighters with no profile page
    f:fighter_linked::boolean                   as fighter_linked,
    trim(f:name::varchar)                       as name,
    f:result::varchar                           as result,
    f:nationality_code::varchar                 as nationality_code,
    -- the fighter's record after this bout, '7-10'
    {{ blank_to_null('f:record_after::varchar') }} as record_after,
    age_text                                    as age_at_fight,
    {{ tapology_age_part('age_text', 'year') }}  as age_years,
    {{ tapology_age_part('age_text', 'month') }} as age_months,
    {{ tapology_age_part('age_text', 'week') }}  as age_weeks,
    {{ tapology_age_part('age_text', 'day') }}   as age_days,
    {{ tapology_cm('get(c:height, lr)::varchar') }} as height_cm,
    {{ tapology_cm('get(c:reach, lr)::varchar') }}  as reach_cm,
    {{ plausible_lbs(first_number('get(c:weigh_in_result, lr)::varchar')) }} as weigh_in_lbs,
    -- '-210 (Moderate Favorite)': the American moneyline
    try_to_number(regexp_substr(get(c:betting_odds, lr)::varchar, '^[+-]?[0-9]+')) as moneyline,
    -- Champion, Challenger, Vacant, Interim Champion, ...
    get(c:title, lr)::varchar                   as title_role
from aged
