-- One row per event page, every status and sport. Scope (Completed, pro MMA) is
-- applied in REFINED. event_slug is the key: 5 legacy pages have no event_id.
select
    payload:event_slug::varchar                 as event_slug,
    payload:event_id::int                       as event_id,
    trim(payload:event_name::varchar)           as event_name,
    payload:status::varchar                     as status,
    payload:event_date_index::date              as event_date,
    -- full, assumed_year (the page gave no year) or from_page
    payload:date_precision::varchar             as date_precision,
    {{ blank_to_null('payload:location_header::varchar') }} as location,
    trim(regexp_substr(payload:location_header::varchar, '[^,]+$')) as country,
    payload:region_slug::varchar                as region_slug,
    {{ blank_to_null('payload:production:venue::varchar') }} as venue,
    {{ blank_to_null('payload:production:enclosure::varchar') }} as enclosure,
    try_to_number(replace(regexp_substr(payload:production:attendance::varchar, '^[0-9,]+'), ',', ''))
                                                as attendance,
    payload:promotion:promotion_id::int         as promotion_id,
    payload:promotion:promotion_slug::varchar   as promotion_slug,
    trim(payload:promotion:name::varchar)       as promotion_name,
    -- the promotion the index listed the event under: the attribution audit
    payload:index_promotion_id::int             as index_promotion_id,
    {{ blank_to_null('payload:organisation::varchar') }} as organisation,
    {{ blank_to_null('payload:ownership::varchar') }} as ownership,
    payload:sport_label::varchar                as sport_label,
    payload:bout_sports                         as bout_sports,
    payload:pro_am                              as pro_am,
    {{ tapology_counts('payload:bout_counts') }} as bout_counts,
    payload:bout_counts_declared                as bout_counts_declared,
    payload:bout_counts_agree::boolean          as bout_counts_agree,
    payload:bout_counts_source::varchar         as bout_counts_source,
    payload:card_note::varchar                  as card_note,
    payload:parse_ok::boolean                   as parse_ok,
    try_to_timestamp_ntz(payload:scraped_at::varchar) as scraped_at,
    _partition_date                             as fetch_date,
    {{ tapology_raw_html_key() }}               as raw_html_key
from {{ ref('lnd_tapology_event_details') }}
{{ latest_fetch('payload:event_slug') }}
