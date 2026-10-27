with fighters as (
    select payload as p, _partition_date, _raw_html_key
    from {{ ref('lnd_ufcstats_fighters') }}
    {{ latest_fetch('payload:ufcstats_fighter_id') }}
)
select
    p:ufcstats_fighter_id::varchar              as ufcstats_fighter_id,
    trim(p:name::varchar)                       as name,
    {{ blank_to_null('p:nickname::varchar') }}  as nickname,
    try_to_date(p:dob::varchar)                 as dob,
    p:height_in::int                            as height_in,
    p:reach_in::int                             as reach_in,
    {{ plausible_lbs(first_number('p:weight::varchar')) }} as weight_lbs,
    {{ blank_to_null('p:stance::varchar') }}    as stance,
    -- '5-3-0' or '5-3-0 (1 NC)': wins-losses-draws, no contests apart
    split_part(p:record::varchar, '-', 1)::int  as wins,
    split_part(p:record::varchar, '-', 2)::int  as losses,
    regexp_substr(p:record::varchar, '^[0-9]+-[0-9]+-([0-9]+)', 1, 1, 'e', 1)::int as draws,
    coalesce(regexp_substr(p:record::varchar, '\\(([0-9]+) NC\\)', 1, 1, 'e', 1)::int, 0) as no_contests,
    -- career averages as UFC Stats shows them; percentages as fractions
    {{ first_number('p:career:slpm::varchar') }}            as sig_str_landed_per_min,
    {{ first_number('p:career:"str. acc."::varchar') }} / 100 as sig_str_accuracy,
    {{ first_number('p:career:sapm::varchar') }}            as sig_str_absorbed_per_min,
    {{ first_number('p:career:"str. def"::varchar') }} / 100  as sig_str_defence,
    {{ first_number('p:career:"td avg."::varchar') }}       as td_per_15_min,
    {{ first_number('p:career:"td acc."::varchar') }} / 100   as td_accuracy,
    {{ first_number('p:career:"td def."::varchar') }} / 100   as td_defence,
    {{ first_number('p:career:"sub. avg."::varchar') }}     as sub_attempts_per_15_min,
    _partition_date                             as fetch_date,
    _raw_html_key                               as raw_html_key
from fighters
