-- UFC decisions with no UFC Stats fight and no override saying why: a new card's
-- names the match could not settle. Look each one up and add it to
-- seeds/xref_decision_ufcstats_fight_overrides.csv with its source.
{{ config(warn_if='>0', error_if='>20') }}

select d.decision_id, d.event_date, d.event_name, d.fighter_1_name, d.fighter_2_name
from {{ ref('cln_mmadecisions_decisions') }} d
where d.promotion_logo = 'ufc'
  and not exists (select 1 from {{ ref('xref_decision_ufcstats_fight') }} x where x.decision_id = d.decision_id)
  and not exists (select 1 from {{ ref('xref_decision_ufcstats_fight_overrides') }} o where o.decision_id = d.decision_id)
