-- One row per judge, plus the average judge and the crowd: what they reward and how
-- they score (DESIGN.md §8). The p_a_wins_ctrl_* curve is one fixed round, A out-struck
-- 20-25 with 0-4 minutes more control; p_10_8_* are a dominant round and a control
-- grind. style_1 and style_2 are the judge's style vector (0 = the average judge):
-- shrunk toward it, so a judge with few rounds is close to it by construction.
select * from {{ source('judge_lens', 'judge_lens_judges') }}
