"""The training rows: one per rater per round, with both fighters' stats.

Every row the fact marks usable (exclusion_reason is NULL) from 2010 on: before
2010 MMADecisions has almost no round scores. Column names come back lower case.
"""
import pandas as pd

STATS = ["knockdowns", "sig_str_landed", "sig_str_attempted", "non_sig_str_landed", "head_landed",
         "body_landed", "leg_landed", "ground_landed", "td_landed", "td_attempted", "sub_attempts",
         "reversals", "ctrl_seconds"]
LABELS = ["p_7_10", "p_8_10", "p_9_10", "p_10_10", "p_10_9", "p_10_8", "p_10_7"]   # margin -3 .. +3

QUERY = f"""
select
    j.ufcstats_fight_id, j.round, j.rater_key, j.rater_kind, j.rater_id, j.rater_name,
    j.decision_id, j.event_date, j.period, j.cv_fold, j.margin, j.label_weight, fa.name as fighter_a_name,
    {", ".join(f"j.{c}" for c in LABELS)},
    {", ".join(f"a.{c} as a_{c}" for c in STATS)},
    {", ".join(f"b.{c} as b_{c}" for c in STATS)}
from refined.fct_judge_round_score j
join refined.fct_ufc_round_stats a
  on a.ufcstats_fight_id = j.ufcstats_fight_id and a.round = j.round and a.ufcstats_fighter_id = j.fighter_a_id
join refined.fct_ufc_round_stats b
  on b.ufcstats_fight_id = j.ufcstats_fight_id and b.round = j.round and b.ufcstats_fighter_id = j.fighter_b_id
left join cleansing.cln_ufcstats_fighters fa on fa.ufcstats_fighter_id = j.fighter_a_id
where j.exclusion_reason is null
  and j.event_date >= '2010-01-01'
"""


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Lower-case names, plain dtypes, and the year the era buckets need."""
    df = df.rename(columns=str.lower).copy()
    for c in [*LABELS, *[f"{s}_{x}" for s in "ab" for x in STATS], "label_weight"]:
        df[c] = pd.to_numeric(df[c]).astype(float)
    df["round"] = df["round"].astype(int)
    df["cv_fold"] = df["cv_fold"].astype(int)
    df["year"] = pd.to_datetime(df["event_date"]).dt.year
    return df.sort_values(["ufcstats_fight_id", "round", "rater_key"]).reset_index(drop=True)


def from_snowpark(session) -> pd.DataFrame:
    return prepare(session.sql(QUERY).to_pandas())


def from_cursor(cur) -> pd.DataFrame:
    cur.execute(QUERY)
    return prepare(pd.DataFrame(cur.fetchall(), columns=[d[0] for d in cur.description]))
