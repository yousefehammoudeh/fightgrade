-- One row per article (or profile page) per site, from its newest sitemap read.
-- Vox (until 2025-08) and WordPress number articles separately, so the key
-- carries the era. Vox ids are 6-8 digits, its 6-digit ones all 2003-12
-- (156110-205618); WordPress's are 5-6 digits from 305646 up. So a dated URL,
-- or any id of 7+ digits, is Vox's: that also merges an old article moved to a
-- WordPress-style URL (/ufc/24382093/slug) with its dated original.
with typed as (
    select
        payload:site::varchar                       as site,
        payload:url::varchar                        as url,
        {{ blank_to_null('payload:section::varchar') }} as section,
        payload:article_id::varchar                 as article_id,
        -- one URL is dated year 2 (/0002/3/2/...): no date, so lastmod stands in
        iff(payload:url_date::date >= '2000-01-01', payload:url_date::date, null) as url_date,
        payload:slug::varchar                       as slug,
        try_to_timestamp_tz(payload:lastmod::varchar) as lastmod,
        payload:sitemap_month::varchar              as sitemap_month,
        _partition_date                             as fetch_date,
        _raw_html_key                               as raw_xml_key,
        _loaded_at,
        _object_key
    from {{ ref('lnd_news_urls') }}
),
eras as (
    select *, iff(url_date is not null or length(article_id) >= 7 or article_id is null, 'vox', 'wp') as url_era
    from typed
)
select
    site || ':' || url_era || ':' || coalesce(article_id, url) as news_url_id,
    site,
    url_era,
    url,
    section,
    article_id,
    url_date,
    slug,
    lastmod,
    sitemap_month,
    fetch_date,
    raw_xml_key
from eras
qualify row_number() over (partition by news_url_id
                           order by fetch_date desc, lastmod desc nulls last, _loaded_at desc, _object_key desc) = 1
