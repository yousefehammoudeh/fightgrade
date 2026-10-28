-- One row per fighter: decisions won against what an average panel would have given
-- on the same round statistics. In sample, so it understates luck a little.
select * from {{ source('judge_lens', 'judge_lens_fighters') }}
