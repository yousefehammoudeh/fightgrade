{#
  MMADecisions names a fight's winner by surname ("Silva", "DRAW", or for a fan
  total "Dawson defeats Aliev"). Resolve it against the decision's two fighters:
  1 or 2 when exactly one fighter's name ends with it, NULL when it is a draw or
  ambiguous (two fighters named Silva). `name_1`/`name_2` are the decision's
  fighter names; pair it with mmadecisions_is_draw for the draw case.
#}
{% macro mmadecisions_winner_token(expr) -%}
case
    when {{ expr }} ilike '% defeats %' then split_part({{ expr }}, ' defeats ', 1)
    when {{ expr }} ilike '% draws with %' then 'Draw'
    else trim({{ expr }})
end
{%- endmacro %}

{% macro mmadecisions_side(token, name_1, name_2) -%}
case
    when upper({{ token }}) = 'DRAW' then null
    when endswith(upper({{ name_1 }}), upper({{ token }})) and not endswith(upper({{ name_2 }}), upper({{ token }})) then 1
    when endswith(upper({{ name_2 }}), upper({{ token }})) and not endswith(upper({{ name_1 }}), upper({{ token }})) then 2
end
{%- endmacro %}

{% macro mmadecisions_is_draw(token) -%}
upper({{ token }}) = 'DRAW'
{%- endmacro %}

{# A round score a judge can give, once deductions are added back: the winner
   on 10, the loser on 7-10 (10-10 an even round). #}
{% macro valid_round_score(a, b) -%}
(greatest({{ a }}, {{ b }}) = 10 and least({{ a }}, {{ b }}) >= 7)
{%- endmacro %}
