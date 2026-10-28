-- One row per decision, seat and replacement: the panel's result with another
-- active judge, the average judge or the crowd in that seat, the rounds as they were.
select * from {{ source('judge_lens', 'judge_lens_swaps') }}
