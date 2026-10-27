-- One row per bout on an event page, from the newest fetch of that page, so a
-- bout dropped from a card on a later fetch is gone. Every sport and level.
select
    payload:bout_slug::varchar                  as bout_slug,
    payload:bout_id::int                        as bout_id,
    payload:event_slug::varchar                 as event_slug,
    payload:event_id::int                       as event_id,
    payload:sport::varchar                      as sport,
    payload:sport_base::varchar                 as sport_base,
    -- Professional, Amateur or Juniors
    payload:level::varchar                      as level,
    payload:billing::varchar                    as billing,
    payload:card_position::int                  as card_position,
    -- order on the card, with where it came from: page, derived
    payload:order_effective::int                as card_order,
    payload:order_source::varchar               as card_order_source,
    {{ blank_to_null('payload:method::varchar') }} as method,
    payload:finish::boolean                     as is_finish,
    payload:finish_round::int                   as finish_round,
    {{ clock_seconds('payload:finish_time::varchar') }} as finish_round_seconds,
    -- '3 Rounds, 15:00 Total', '0:38 Round 1 of 3': the total where the page gives one
    {{ clock_seconds("regexp_substr(payload:duration::varchar, '([0-9]+:[0-9]{2}) Total', 1, 1, 'e', 1)") }}
                                                as total_seconds,
    payload:duration::varchar                   as duration,
    payload:round_format::varchar               as round_format,
    payload:scheduled_rounds::int               as scheduled_rounds,
    {{ blank_to_null('payload:title_bout::varchar') }} as title_bout,
    -- title or tournament
    payload:title_kind::varchar                 as title_kind,
    -- the badge: pounds ('135') or a class code ('HW')
    payload:weight_badge::varchar               as weight_badge,
    iff(payload:weight_badge_kind::varchar = 'numeric',
        {{ plausible_lbs("try_to_number(payload:weight_badge::varchar)") }}, null) as weight_lbs,
    payload:modified_rules::boolean             as modified_rules,
    {{ blank_to_null('payload:bout_notes::varchar') }} as bout_notes,
    _partition_date                             as fetch_date,
    {{ tapology_raw_html_key() }}               as raw_html_key
from {{ ref('lnd_tapology_bouts') }}
{{ latest_fetch('payload:event_slug') }}
