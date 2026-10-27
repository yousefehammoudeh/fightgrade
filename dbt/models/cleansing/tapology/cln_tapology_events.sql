-- The event index as listed (the 2026-08 bulk load; the crawler reads event pages
-- directly). Promotion and region as the index had them.
select
    payload:event_slug::varchar                 as event_slug,
    payload:event_id::int                       as event_id,
    trim(payload:event_name::varchar)           as event_name,
    payload:promotion_id::int                   as promotion_id,
    payload:region_slug::varchar                as region_slug,
    payload:region_name::varchar                as region_name,
    -- 'derived_from_location' marks the 2,088 regions assigned after the crawl
    payload:region_source::varchar              as region_source,
    payload:title_fight::boolean                as has_title_fight,
    {{ blank_to_null('payload:title_name::varchar') }} as title_name,
    -- 'sitemap+detail_backfill' marks events found through the sitemap
    payload:source::varchar                     as listing_source,
    _partition_date                             as fetch_date
from {{ ref('lnd_tapology_events') }}
{{ latest_fetch('payload:event_slug') }}
