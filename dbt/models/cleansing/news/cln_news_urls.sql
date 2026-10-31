-- One row per article (or profile page) per site, from its newest sitemap read.
-- Vox's own URLs (until the move to WordPress) and WordPress's number their
-- articles separately, so the key carries the era: a dated URL is Vox's.
with typed as (
    select
        payload:site::varchar                       as site,
        payload:url::varchar                        as url,
        {{ blank_to_null('payload:section::varchar') }} as section,
        payload:article_id::varchar                 as article_id,
        payload:url_date::date                      as url_date,
        payload:slug::varchar                       as slug,
        try_to_timestamp_tz(payload:lastmod::varchar) as lastmod,
        payload:sitemap_month::varchar              as sitemap_month,
        _partition_date                             as fetch_date,
        _raw_html_key                               as raw_xml_key,
        _loaded_at,
        _object_key
    from {{ ref('lnd_news_urls') }}
)
select
    site || ':' || iff(url_date is not null, 'vox', 'wp') || ':' || coalesce(article_id, url) as news_url_id,
    site,
    iff(url_date is not null, 'vox', 'wp')          as url_era,
    url,
    section,
    article_id,
    url_date,
    slug,
    lastmod,
    sitemap_month,
    fetch_date,
    raw_xml_key
from typed
qualify row_number() over (partition by news_url_id
                           order by fetch_date desc, lastmod desc nulls last, _loaded_at desc, _object_key desc) = 1
