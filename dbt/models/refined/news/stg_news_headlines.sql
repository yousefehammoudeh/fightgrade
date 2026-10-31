-- Every news article as a headline: its slug as words, its day, and a
-- deliberately loose keyword pass marking candidates for the classifier
-- (int_news_events). The pass trades precision for recall; the gold set
-- (seed news_event_labels) measures what it drops. Profile pages (/fighter/,
-- /fight/<a>-vs-<b>) are not articles and are left out.
--
-- published_date: the day in a Vox URL; a WordPress URL has none, so its
-- lastmod's day stands in (date_source = 'lastmod'), which is the last edit.
{% set families = {
    'retirement': "retir|hang(s|ing)? up (the|his|her) gloves|calls? (it )?a career|final fight|last fight|farewell|walks? away|step(s|ping)? away from|comeback|come back|return|back in the (octagon|cage)|un ?retir",
    'withdrawal': "withdr|pulls? out|pulled|out of|forced out|injur|hurt|scrap|cancel|off (the |of )?(card|fight|ufc|bout)|postpone|falls? (apart|through)|fell (apart|through|off)|nixed|axed|illness|sick|hospital|visa|deleted",
    'replacement': "replac|steps? in|stepping in|short ?notice|late ?notice|new opponent|fill(s|ing)? in|late change",
    'weight_miss': "miss(es|ed)? weight|weight miss|miss(es|ed)? (the )?mark|overweight|catch ?weight|weigh ?ins?|on the scale|weight cut|hydration",
    'suspension': "suspen|usada|drug test|test(s|ed)? positive|positive test|flagged|banned|ban for|doping|adverse|steroid|nsac|commission|wada|ped",
    'release': "releas|cut from|cut by|parts? ways|parted ways|let go|free agent|signs|signed|signing|inks|new deal|contract|debut",
} %}
with articles as (
    select *
    from {{ ref('cln_news_urls') }}
    where article_id is not null
      and slug is not null
      and coalesce(section, '') not in ('fight', 'fighter', 'fighters')
),
worded as (
    select
        news_url_id,
        site,
        url_era,
        url,
        section,
        article_id,
        coalesce(url_date, lastmod::date)               as published_date,
        iff(url_date is not null, 'url', 'lastmod')     as date_source,
        lower(replace(slug, '-', ' '))                  as headline,
        fetch_date
    from articles
),
flagged as (
    select
        *,
        array_construct_compact(
        {%- for name, pattern in families.items() %}
            iff(regexp_instr(headline, '(^| )({{ pattern }})') > 0, '{{ name }}', null){{ "," if not loop.last }}
        {%- endfor %}
        )                                               as candidate_types
    from worded
)
select
    news_url_id,
    site,
    url_era,
    url,
    section,
    article_id,
    published_date,
    date_source,
    year(published_date)                                as published_year,
    headline,
    array_size(candidate_types) > 0                     as is_candidate,
    candidate_types,
    fetch_date
from flagged
