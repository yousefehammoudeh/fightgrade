-- Fighter events in the news: one row per headline the classifier says is one.
--
-- Two stages (pipeline/news_classify.py): every candidate headline is read by a
-- local model from its slug; the ones it flags as a retirement, return,
-- withdrawal or replacement have their article read too (pipeline/news_articles.py),
-- and the article's answer replaces the slug's. Answers are classified once per
-- (model, prompt version) in LANDING; the vars choose which ones count. A headline
-- about another sport is none whatever the model said (not_mma).
--
-- A claim, not a fact: whether a disruption happened is settled against the fight
-- data downstream (README, step 6), and a retirement holds only until the next bout.
{% set model = var('news_model', 'qwen3:4b') %}
{% set slug_prompt = var('news_slug_prompt', 'v2') %}
{% set article_prompt = var('news_article_prompt', 'a2') %}
with answers as (
    select
        payload:news_url_id::varchar                as news_url_id,
        payload:prompt_version::varchar             as prompt_version,
        payload:event_type::varchar                 as event_type,
        payload:status::varchar                     as status,
        payload:fighters                            as fighters,
        payload:replaced_fighter::varchar           as replaced_fighter,
        payload:opponent::varchar                   as opponent,
        payload:event::varchar                      as event,
        payload:reason::varchar                     as reason,
        coalesce(payload:not_mma::boolean, false)   as not_mma,
        payload:classified_on::date                 as classified_on
    from {{ ref('lnd_news_events') }}
    where payload:model::varchar = '{{ model }}'
      and payload:prompt_version::varchar in ('{{ slug_prompt }}', '{{ article_prompt }}')
      and payload:event_type is not null
    qualify row_number() over (partition by news_url_id, prompt_version order by _loaded_at desc, _object_key desc) = 1
),
articles as (
    select
        payload:news_url_id::varchar                as news_url_id,
        payload:headline::varchar                   as article_headline,
        try_to_timestamp_tz(payload:date_published::varchar) as published_at
    from {{ ref('lnd_news_articles') }}
    qualify row_number() over (partition by news_url_id order by _loaded_at desc, _object_key desc) = 1
),
chosen as (
    select a.*, iff(a.prompt_version = '{{ article_prompt }}', 'article', 'slug') as source
    from answers a
    qualify row_number() over (partition by a.news_url_id
                               order by iff(a.prompt_version = '{{ article_prompt }}', 0, 1)) = 1
)
select
    h.news_url_id,
    h.site,
    h.url,
    h.published_date,
    ar.published_at,
    h.headline,
    ar.article_headline,
    c.event_type,
    c.status,
    c.fighters,
    nullif(c.replaced_fighter, '')                  as replaced_fighter,
    nullif(c.opponent, '')                          as opponent,
    nullif(c.event, '')                             as event,
    nullif(c.reason, '')                            as reason,
    c.source,
    '{{ model }}'                                   as model,
    c.prompt_version,
    c.classified_on
from chosen c
join {{ ref('stg_news_headlines') }} h using (news_url_id)
left join articles ar using (news_url_id)
where c.event_type <> 'none' and not c.not_mma
