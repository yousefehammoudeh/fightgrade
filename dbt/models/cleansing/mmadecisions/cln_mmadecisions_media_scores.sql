-- One row per media scorecard. score_1/score_2 are (fighter 1, fighter 2).
with decisions as (
    select payload as p
    from {{ ref('lnd_mmadecisions_decisions') }}
    {{ latest_fetch('payload:decision_id') }}
)
select
    m.payload:decision_id::varchar              as decision_id,
    {{ blank_to_null('m.payload:writer::varchar') }} as writer,
    {{ blank_to_null('m.payload:outlet::varchar') }} as outlet,
    m.payload:score_1::int                      as score_1,
    m.payload:score_2::int                      as score_2,
    {{ mmadecisions_side('m.payload:winner::varchar', 'd.p:fighter_1:name::varchar', 'd.p:fighter_2:name::varchar') }}
                                                as winner_side,
    {{ mmadecisions_is_draw('m.payload:winner::varchar') }} as is_draw,
    {{ blank_to_null('m.payload:source_url::varchar') }} as source_url
from {{ ref('lnd_mmadecisions_media_scores') }} m
join decisions d on d.p:decision_id = m.payload:decision_id
{{ latest_fetch('m.payload:decision_id') }}
