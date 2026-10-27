-- One row per promotion: the index row, with its page's details where fetched.
with promotions as (
    select payload as p, _partition_date
    from {{ ref('lnd_tapology_promotions') }}
    {{ latest_fetch('payload:id') }}
),
details as (
    select payload as p
    from {{ ref('lnd_tapology_promotion_details') }}
    {{ latest_fetch('payload:promotion_id') }}
)
select
    pr.p:id::int                                as promotion_id,
    pr.p:slug::varchar                          as promotion_slug,
    trim(pr.p:name::varchar)                    as promotion_name,
    {{ blank_to_null('d.p:acronyms::varchar') }} as acronyms,
    {{ blank_to_null('d.p:also_known_as::varchar') }} as also_known_as,
    {{ blank_to_null('d.p:headquarters::varchar') }} as headquarters,
    trim(regexp_substr(d.p:headquarters::varchar, '[^,]+$')) as country,
    d.p is not null                             as has_details,
    pr._partition_date                          as fetch_date
from promotions pr
left join details d on d.p:promotion_id::int = pr.p:id::int
