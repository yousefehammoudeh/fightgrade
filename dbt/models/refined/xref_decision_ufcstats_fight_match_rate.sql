-- How much of MMADecisions the fight match covers, per promotion logo. Only UFC
-- decisions are expected in UFC Stats, so only they get a match rate; other
-- promotions match where UFC Stats happens to carry the event (Strikeforce, WEC).
-- Decisions an override records as absent from UFC Stats leave the denominator.
with decisions as (
    select decision_id, promotion_logo from {{ ref('cln_mmadecisions_decisions') }}
),
absent as (
    select decision_id from {{ ref('xref_decision_ufcstats_fight_overrides') }}
    where ufcstats_fight_id is null
),
counted as (
    select
        d.promotion_logo,
        count(*)                                        as decisions,
        count(a.decision_id)                            as known_absent,
        count(x.decision_id)                            as matched,
        count_if(x.match_method = 'override')           as matched_by_override,
        count_if(x.scorecards_agree)                    as scorecards_agree,
        count(x.scorecards_agree)                       as scorecards_compared
    from decisions d
    left join absent a using (decision_id)
    left join {{ ref('xref_decision_ufcstats_fight') }} x using (decision_id)
    group by 1
)
select
    promotion_logo,
    promotion_logo = 'ufc'                              as expected_in_ufcstats,
    decisions,
    known_absent,
    matched,
    matched_by_override,
    decisions - known_absent - matched                  as unmatched,
    iff(promotion_logo = 'ufc', matched / nullif(decisions - known_absent, 0), null)
                                                        as match_rate,
    scorecards_agree / nullif(scorecards_compared, 0)   as scorecards_agree_rate
from counted
