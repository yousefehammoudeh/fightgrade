-- The fan scorecards the page summarises: per round (scope 'round') and for the
-- fight (scope 'total'), each as a share of submissions. score_hi/score_lo are
-- the outcome's winner's and loser's points, not fighter 1's and 2's.
with decisions as (
    select payload as p
    from {{ ref('lnd_mmadecisions_decisions') }}
    {{ latest_fetch('payload:decision_id') }}
)
select
    f.payload:decision_id::varchar              as decision_id,
    f.payload:scope::varchar                    as scope,
    f.payload:round::int                        as round,
    trim(f.payload:outcome::varchar)            as outcome,
    {{ mmadecisions_side(mmadecisions_winner_token('f.payload:outcome::varchar'),
                         'd.p:fighter_1:name::varchar', 'd.p:fighter_2:name::varchar') }}
                                                as winner_side,
    {{ mmadecisions_is_draw(mmadecisions_winner_token('f.payload:outcome::varchar')) }} as is_draw,
    f.payload:score_hi::int                     as score_hi,
    f.payload:score_lo::int                     as score_lo,
    f.payload:pct::number(5, 1) / 100           as share,
    f.payload:est_count::int                    as est_count
from {{ ref('lnd_mmadecisions_fan_scores') }} f
join decisions d on d.p:decision_id = f.payload:decision_id
{{ latest_fetch('f.payload:decision_id') }}
