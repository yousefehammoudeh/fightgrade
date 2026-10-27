-- Title-bout pages: the belt at stake and who held it, including the cancelled
-- title bouts that never reached a card.
select
    payload:bout_slug::varchar                  as bout_slug,
    payload:bout_id::int                        as bout_id,
    payload:event_slug::varchar                 as event_slug,
    payload:event_date::date                    as event_date,
    payload:event_status::varchar               as event_status,
    payload:sport::varchar                      as sport,
    payload:fighters[0]::varchar                as fighter_1_slug,
    payload:fighters[1]::varchar                as fighter_2_slug,
    payload:title_on_line::varchar              as title_on_line,
    payload:title_bout_from_card::varchar       as title_bout_from_card,
    -- held, vacant, absent, ...
    payload:belt_status_kind::varchar           as belt_status_kind,
    {{ blank_to_null('payload:belt_status_before_fight::varchar') }} as belt_status_before_fight,
    {{ blank_to_null('payload:belt_holder::varchar') }} as belt_holder,
    {{ blank_to_null('payload:tournament::varchar') }} as tournament,
    {{ blank_to_null('payload:tournament_round::varchar') }} as tournament_round,
    payload:parse_ok::boolean                   as parse_ok,
    try_to_timestamp_ntz(payload:scraped_at::varchar) as scraped_at,
    _partition_date                             as fetch_date,
    {{ tapology_raw_html_key() }}               as raw_html_key
from {{ ref('lnd_tapology_bout_details') }}
{{ latest_fetch('payload:bout_slug') }}
