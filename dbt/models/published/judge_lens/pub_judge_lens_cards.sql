-- One row per decision and judge: the card they handed in, and the three cards the
-- model expected from them (A's margin over the fight, deductions not yet applied).
select * from {{ source('judge_lens', 'judge_lens_cards') }}
