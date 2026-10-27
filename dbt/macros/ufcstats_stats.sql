{#
  One row per fighter per round (per_round=true) or per fight, with the totals
  table's counts and the significant-strike breakdowns by target and position
  joined on. Each comes from the newest fetch of its fight page. Landed and
  attempted are kept apart; the "x of y" strings are dropped.
#}
{% macro ufcstats_stats(per_round) %}
{%- set counts = ref('lnd_ufcstats_fight_rounds') if per_round else ref('lnd_ufcstats_fight_totals') -%}
{%- set round_filter = "payload:round::varchar <> 'totals'" if per_round else "payload:round::varchar = 'totals'" -%}
{%- set keys = ['fight_id', 'fighter_id'] + (['round'] if per_round else []) %}
with counts as (
    select payload as p, _partition_date, _raw_html_key
    from {{ counts }}
    {{ latest_fetch('payload:ufcstats_fight_id') }}
),
by_target as (
    select payload as p
    from {{ ref('lnd_ufcstats_sig_strikes_by_target') }}
    where {{ round_filter }}
    {{ latest_fetch('payload:ufcstats_fight_id') }}
),
by_position as (
    select payload as p
    from {{ ref('lnd_ufcstats_sig_strikes_by_position') }}
    where {{ round_filter }}
    {{ latest_fetch('payload:ufcstats_fight_id') }}
)
select
    c.p:ufcstats_fight_id::varchar        as ufcstats_fight_id,
    c.p:ufcstats_fighter_id::varchar      as ufcstats_fighter_id,
    {% if per_round -%}
    c.p:round::int                        as round,
    {% endif -%}
    c.p:kd::int                           as knockdowns,
    c.p:sig_str_land::int                 as sig_str_landed,
    c.p:sig_str_att::int                  as sig_str_attempted,
    c.p:total_str_land::int               as total_str_landed,
    c.p:total_str_att::int                as total_str_attempted,
    c.p:td_land::int                      as td_landed,
    c.p:td_att::int                       as td_attempted,
    c.p:sub_att::int                      as sub_attempts,
    c.p:rev::int                          as reversals,
    -- '--' on the page (no control time recorded, mostly pre-2008) is NULL, not 0
    {{ clock_seconds('c.p:ctrl::varchar') }} as ctrl_seconds,
    t.p:head_land::int                    as head_landed,
    t.p:head_att::int                     as head_attempted,
    t.p:body_land::int                    as body_landed,
    t.p:body_att::int                     as body_attempted,
    t.p:leg_land::int                     as leg_landed,
    t.p:leg_att::int                      as leg_attempted,
    s.p:distance_land::int                as distance_landed,
    s.p:distance_att::int                 as distance_attempted,
    s.p:clinch_land::int                  as clinch_landed,
    s.p:clinch_att::int                   as clinch_attempted,
    s.p:ground_land::int                  as ground_landed,
    s.p:ground_att::int                   as ground_attempted,
    c._partition_date                     as fetch_date,
    c._raw_html_key                       as raw_html_key
from counts c
left join by_target t
    on t.p:ufcstats_fight_id = c.p:ufcstats_fight_id
   and t.p:ufcstats_fighter_id = c.p:ufcstats_fighter_id
   {% if per_round %}and t.p:round::int = c.p:round::int{% endif %}
left join by_position s
    on s.p:ufcstats_fight_id = c.p:ufcstats_fight_id
   and s.p:ufcstats_fighter_id = c.p:ufcstats_fighter_id
   {% if per_round %}and s.p:round::int = c.p:round::int{% endif %}
{% endmacro %}
