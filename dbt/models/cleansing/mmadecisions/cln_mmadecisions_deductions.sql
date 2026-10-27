-- Point deductions, one row per deduction, with the penalised fighter resolved
-- from the surname the page gives.
with decisions as (
    select payload as p
    from {{ ref('lnd_mmadecisions_decisions') }}
    {{ latest_fetch('payload:decision_id') }}
)
select
    d.p:decision_id::varchar                    as decision_id,
    x.index + 1                                 as deduction_slot,
    x.value:round::int                          as round,
    x.value:points::int                         as points,
    {{ blank_to_null('x.value:reason::varchar') }} as reason,
    trim(x.value:fighter::varchar)              as fighter_surname,
    {{ mmadecisions_side('x.value:fighter::varchar', 'd.p:fighter_1:name::varchar', 'd.p:fighter_2:name::varchar') }}
                                                as fighter_side
from decisions d,
    lateral flatten(input => d.p:deductions) x
