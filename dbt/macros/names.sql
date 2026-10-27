{#
  Comparing fighter names across sources. name_key folds a name to lower-case
  ASCII letters and single spaces ('Márcio Alexandre Jr.' -> 'marcio alexandre',
  "Sangcha'an" -> 'sangchaan', 'Jeong-yeong' -> 'jeong yeong').
#}
{% macro name_key(expr) -%}
trim(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
    lower(translate({{ expr }},
        'ÁÀÂÄÃÅáàâäãåÉÈÊËéèêëÍÌÎÏíìîïÓÒÔÖÕØóòôöõøÚÙÛÜúùûüÝýÿÑñÇçŁłŠšŽžĆćČč',
        'AAAAAAaaaaaaEEEEeeeeIIIIiiiiOOOOOOooooooUUUUuuuuYyyNnCcLlSsZzCcCc')),
    '-', ' '), '[^a-z ]', ''), ' (jr|junior|sr|ii|iii|iv)$', ''), ' +', ' '))
{%- endmacro %}

{#
  How alike two name keys are, from 3 down to 0:
    3  the same name, or the same letters in another order or spacing
       ('weili zhang' / 'zhang weili', 'heili alateng' / 'alatengheili')
    2  the same surname
    1  a shared name of three letters or more ('tiago trator' / 'tiago dos santos e silva')
#}
{% macro name_similarity(a, b) -%}
case
    when {{ a }} = {{ b }}
      or array_to_string(array_sort(regexp_substr_all(replace({{ a }}, ' ', ''), '.')), '')
       = array_to_string(array_sort(regexp_substr_all(replace({{ b }}, ' ', ''), '.')), '') then 3
    when split_part({{ a }}, ' ', -1) = split_part({{ b }}, ' ', -1) then 2
    when arrays_overlap(filter(split({{ a }}, ' '), t -> length(t) >= 3),
                        filter(split({{ b }}, ' '), t -> length(t) >= 3)) then 1
    else 0
end
{%- endmacro %}

{#
  A pairing's tier from its two fighters' similarities (lower is stronger):
    1 both names; 2 both surnames; 3 one name and a shared name on the other side;
    4 one name only. NULL when neither fighter's name matches.
#}
{% macro pairing_tier(x, y) -%}
case
    when least({{ x }}, {{ y }}) >= 3 then 1
    when least({{ x }}, {{ y }}) >= 2 then 2
    when greatest({{ x }}, {{ y }}) >= 3 and least({{ x }}, {{ y }}) >= 1 then 3
    when greatest({{ x }}, {{ y }}) >= 3 then 4
end
{%- endmacro %}
