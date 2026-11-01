-- One row per bout change in each UFC event article's Background section, as
-- the newest run that read the article parsed it.
--
-- Every run that reads an article (the weekly ±60 days, a backfill, a replay of
-- the archive with a newer parser) appends all its rows again, so only the rows
-- of the latest run per requested title are kept. A run always parses the newest
-- archived revision, and run ids sort in time. An article whose changes have all
-- been edited out yields no rows, so its older ones stay until it has one again.
--
-- CC BY-SA 4.0: revid and source_url attribute every row to its revision.
with typed as (
    select
        payload:requested_title::varchar            as requested_title,
        payload:page_title::varchar                 as page_title,
        payload:page_id::number                     as page_id,
        payload:revid::number                       as revid,
        payload:event_label::varchar                as event_label,
        payload:event_date::date                    as event_date,
        payload:paragraph::number                   as paragraph,
        payload:sentence_index::number              as sentence_index,
        payload:sentence::varchar                   as sentence,
        payload:change_type::varchar                as change_type,
        payload:withdrew::boolean                   as withdrew,
        payload:replaced::boolean                   as replaced,
        payload:cancelled::boolean                  as cancelled,
        payload:moved::boolean                      as moved,
        payload:weighed_lbs::float                  as weighed_lbs,
        {{ blank_to_null('payload:reason::varchar') }} as reason,
        payload:reason_category::varchar            as reason_category,
        payload:weight_class::varchar               as weight_class,
        payload:fighter_a::varchar                  as fighter_a,
        payload:fighter_a_wiki::varchar             as fighter_a_wiki,
        payload:fighter_b::varchar                  as fighter_b,
        payload:fighter_b_wiki::varchar             as fighter_b_wiki,
        payload:withdrawn_fighter::varchar          as withdrawn_fighter,
        payload:withdrawn_fighter_wiki::varchar     as withdrawn_fighter_wiki,
        payload:opponent::varchar                   as opponent,
        payload:replacement_fighter::varchar        as replacement_fighter,
        payload:replacement_fighter_wiki::varchar   as replacement_fighter_wiki,
        payload:refers_to_event::varchar            as refers_to_event,
        payload:citations                           as citations,
        payload:cited_in::varchar                   as cited_in,
        payload:pairing_sentence::varchar           as pairing_sentence,
        payload:pairing_citations                   as pairing_citations,
        payload:source_url::varchar                 as source_url,
        payload:license::varchar                    as license,
        payload:fetch_date::date                    as fetch_date,
        _raw_html_key                               as raw_json_key,
        regexp_substr(_object_key, 'part-([0-9]{8}T[0-9]{6}Z)', 1, 1, 'e') as run_id
    from {{ ref('lnd_wikipedia_bout_changes') }}
),
newest_run as (
    select *
    from typed
    qualify dense_rank() over (partition by requested_title order by run_id desc) = 1
),
-- An early card with no article of its own redirects to a section of "2012 in
-- UFC" and the like; several cards then read the same Background, which belongs
-- to one of them, and nothing says which. Those rows are dropped.
shared_pages as (
    select page_title from newest_run group by 1 having count(distinct requested_title) > 1
),
latest as (
    select * from newest_run where page_title not in (select page_title from shared_pages)
),
-- The earliest day a citation gives: when the change was first reported.
first_cited as (
    select l.requested_title, l.paragraph, l.sentence_index, min(try_to_date(c.value:date_iso::varchar)) as first_cited_on
    from latest l, lateral flatten(input => l.citations) c
    group by 1, 2, 3
)
select
    l.requested_title || ':' || l.paragraph || ':' || l.sentence_index as bout_change_id,
    l.requested_title,
    l.page_title,
    l.page_id,
    l.revid,
    l.event_label,
    l.event_date,
    l.paragraph,
    l.sentence_index,
    l.sentence,
    l.change_type,
    l.withdrew,
    l.replaced,
    l.cancelled,
    l.moved,
    l.weighed_lbs,
    l.reason,
    l.reason_category,
    l.weight_class,
    l.fighter_a,
    l.fighter_a_wiki,
    l.fighter_b,
    l.fighter_b_wiki,
    l.withdrawn_fighter,
    l.withdrawn_fighter_wiki,
    l.opponent,
    l.replacement_fighter,
    l.replacement_fighter_wiki,
    l.refers_to_event,
    f.first_cited_on,
    l.citations,
    l.cited_in,
    l.pairing_sentence,
    l.pairing_citations,
    l.source_url,
    l.license,
    l.fetch_date,
    l.raw_json_key,
    l.run_id
from latest l
left join first_cited f using (requested_title, paragraph, sentence_index)
