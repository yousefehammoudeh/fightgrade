"""The tables Judge Lens reads (DESIGN.md §8): one version's view of every decision.

mode 'publish' (and 'fit', when it promotes a version) replaces each table whole,
in the environment's ML schema; the PUBLISHED dbt models are views over them.

    ML.JUDGE_LENS_ROUNDS    per round and rater (each judge, the crowd, the average
                            judge): the seven scores, the call made and how likely it
                            was, and, for the average judge, what each stat added
    ML.JUDGE_LENS_FIGHTS    per decision: the real panel, an average panel, the crowd,
                            and random panels drawn the way judges are assigned
    ML.JUDGE_LENS_CARDS     per decision and judge: the cards they were likely to give
    ML.JUDGE_LENS_SWAPS     per decision, seat and replacement: the result with another
                            judge, the average judge or the crowd in that seat
    ML.JUDGE_LENS_JUDGES    per judge: what they reward, 10-8s, how often they stand alone
    ML.JUDGE_LENS_FIGHTERS  per fighter: decisions won against what an average panel
                            would have given (decision luck)

In sample: the version has trained on these fights. ML.JUDGE_LENS_PREDICTIONS is
the out-of-sample record. Fight-level numbers come from panel.py's shared effects.
"""
import zlib

import numpy as np
import pandas as pd

from . import panel
from .data import LABELS, STATS
from .metrics import win_prob
from .model import AVERAGE, CROWD

GROUPS = {"kd": "knockdowns", "td": "takedowns", "ctrl": "control", "sub": "sub_attempts", "rev": "reversals",
          "sig_att": "strike_volume", "non_sig": "non_sig_strikes"}     # the rest: head, body, leg = strikes_landed
GROUP_NAMES = ["strikes_landed", "control", "takedowns", "non_sig_strikes", "knockdowns", "sub_attempts",
               "strike_volume", "reversals"]
POOL_YEARS, POOL_MIN_ROUNDS, N_PANELS = 3, 100, 400


def group_of(term: str) -> str:
    for prefix, g in GROUPS.items():
        if term.startswith(prefix):
            return g
    return "strikes_landed"


def contributions(basis, model, rows: pd.DataFrame) -> pd.DataFrame:
    """What each group of stats adds to the average judge's score for A (log-odds)."""
    c = basis.transform(rows) * model.p["beta"]
    groups = np.array([group_of(n) for n in basis.names])
    return pd.DataFrame({f"contrib_{g}": c[:, groups == g].sum(1) for g in GROUP_NAMES}, index=rows.index)


def judge_pool(judges: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """The judges random panels are drawn from: POOL_MIN_ROUNDS+ in the last
    POOL_YEARS, weighted by their rounds, as panels are filled."""
    recent = judges[judges["year"] > judges["year"].max() - POOL_YEARS].dropna(subset=["rater_id"])
    n = recent.groupby("rater_id").size()
    n = n[n >= POOL_MIN_ROUNDS]
    return n.index.to_numpy(object), (n / n.sum()).to_numpy()


def rounds_table(df: pd.DataFrame, basis, model, version: str) -> pd.DataFrame:
    who = np.where(df["rater_kind"] == "crowd", CROWD, df["rater_id"].astype(object)).astype(object)
    P = model.predict(basis.transform(df), who, df["year"].to_numpy())
    A = model.predict(basis.transform(df), np.full(len(df), AVERAGE, dtype=object), df["year"].to_numpy())
    keys = ["decision_id", "ufcstats_fight_id", "ufcstats_event_id", "event_date", "round", "fighter_a_id", "fighter_b_id"]
    out = df[[*keys, "rater_key", "rater_kind", "rater_id", "rater_name", "margin"]].copy()
    out[LABELS] = P
    out["p_a_wins"], out["p_10_8_plus"], out["average_judge_p_a_wins"] = win_prob(P), P[:, [0, 1, 5, 6]].sum(1), win_prob(A)
    a_share = df[LABELS[4:]].sum(1) / (df[LABELS[:3]].sum(1) + df[LABELS[4:]].sum(1))
    out["actual_p_a_wins"] = np.where(df["rater_kind"] == "judge", (df["margin"] > 0).astype(float), a_share)
    called = (df["rater_kind"] == "judge") & (df["margin"] != 0)
    side_own, side_avg = np.where(df["margin"] > 0, win_prob(P), 1 - win_prob(P)), np.where(df["margin"] > 0, win_prob(A), 1 - win_prob(A))
    out["p_call"] = np.where(called, side_own, np.nan)                 # how likely the call made was, for this judge
    out["p_call_average"] = np.where(called, side_avg, np.nan)         # and for the average judge
    avg = df[df["rater_kind"] == "judge"].drop_duplicates(["ufcstats_fight_id", "round"])
    A_avg = model.predict(basis.transform(avg), np.full(len(avg), AVERAGE, dtype=object), avg["year"].to_numpy())
    rows = avg[keys].copy()
    rows["rater_key"], rows["rater_kind"], rows["rater_id"], rows["rater_name"], rows["margin"] = "average", "average", AVERAGE, "Average judge", np.nan
    rows[LABELS] = A_avg
    rows["p_a_wins"], rows["p_10_8_plus"], rows["average_judge_p_a_wins"] = win_prob(A_avg), A_avg[:, [0, 1, 5, 6]].sum(1), win_prob(A_avg)
    rows["actual_p_a_wins"] = rows["p_call"] = rows["p_call_average"] = np.nan
    contrib = contributions(basis, model, avg)
    rows = pd.concat([rows, contrib], axis=1)
    rows["score_total"] = contrib.sum(1)
    out = pd.concat([out, rows], ignore_index=True)
    out.insert(0, "model_version", version)
    return out


def fight_tables(df: pd.DataFrame, basis, model, version: str) -> dict[str, pd.DataFrame]:
    judges = df[df["rater_kind"] == "judge"]
    pool, weights = judge_pool(judges)
    rng = np.random.default_rng(0)
    random_panels = [rng.choice(len(pool), size=3, replace=False, p=weights) for _ in range(N_PANELS)]
    fights, cards_, swaps = [], [], []
    for did, g in judges.groupby("decision_id"):
        rounds = g.drop_duplicates("round").sort_values("round")
        R = len(rounds)
        seats = sorted(g["rater_key"].unique())
        if len(seats) != 3 or len(g) != 3 * R or list(rounds["round"]) != list(range(1, R + 1)):
            continue                                    # the fight needs every judge's every round
        seat_rows = [g[g["rater_key"] == s].sort_values("round") for s in seats]
        seat_rater = [r["rater_id"].iloc[0] if isinstance(r["rater_id"].iloc[0], str) else AVERAGE for r in seat_rows]
        cand = list(dict.fromkeys([*seat_rater, *pool, AVERAGE, CROWD]))
        at = {c: i for i, c in enumerate(cand)}
        eta, t1, t2, t3 = panel.round_scores(basis, model, rounds, cand)
        shift = rounds["deducted_a"].fillna(0).sum() - rounds["deducted_b"].fillna(0).sum()
        shared = panel.draws(R, seed=zlib.crc32(str(did).encode()))
        card, dist = panel.cards(eta, t1, t2, t3, shared, shift)
        seat_idx = [at[r] for r in seat_rater]
        real = panel.panel_probs(card, np.array([seat_idx]))[0]
        average = panel.panel_probs(card, np.array([[at[AVERAGE]] * 3]))[0]
        crowd = card[:, at[CROWD]].mean(0)
        rp = panel.panel_probs(card, np.array([[at[pool[i]] for i in p] for p in random_panels]))
        totals = [int((r["score_a"] - r["score_b"]).sum()) for r in seat_rows]
        a_cards, b_cards = sum(t > 0 for t in totals), sum(t < 0 for t in totals)
        first = rounds.iloc[0]
        fights.append({"decision_id": did, "ufcstats_fight_id": first["ufcstats_fight_id"], "ufcstats_event_id": first["ufcstats_event_id"],
                       "event_date": first["event_date"], "fighter_a_id": first["fighter_a_id"], "fighter_b_id": first["fighter_b_id"],
                       "rounds": R, "deducted_a": rounds["deducted_a"].fillna(0).sum(), "deducted_b": rounds["deducted_b"].fillna(0).sum(),
                       "result": "A" if a_cards > b_cards else ("B" if b_cards > a_cards else "draw"),
                       "p_a_wins": real[0], "p_draw": real[1], "p_b_wins": real[2],
                       **{f"p_{k}": v for k, v in panel.decision_types(card, seat_idx).items()},
                       "average_panel_p_a_wins": average[0], "average_panel_p_b_wins": average[2],
                       "crowd_card_p_a": crowd[0], "crowd_card_p_level": crowd[1], "crowd_card_p_b": crowd[2],
                       "random_panels_p_a_median": float(np.median(rp[:, 0])), "random_panels_p_a_p10": float(np.quantile(rp[:, 0], .1)),
                       "random_panels_p_a_p90": float(np.quantile(rp[:, 0], .9)),
                       "random_panels_share_favouring_a": float((rp[:, 0] > rp[:, 2]).mean()), "random_panels_judges": len(pool)})
        offset = 3 * R
        for s, key, rid, rows_, tot in zip(range(3), seats, seat_rater, seat_rows, totals):
            d = dist[:, seat_idx[s]].mean(0)
            top = np.argsort(-d)[:3]
            cards_.append({"decision_id": did, "rater_key": key, "rater_id": rows_["rater_id"].iloc[0], "rater_name": rows_["rater_name"].iloc[0],
                           "actual_card_margin": tot, "p_card_a": card[:, seat_idx[s], 0].mean(), "p_card_level": card[:, seat_idx[s], 1].mean(),
                           **{f"likely_margin_{i + 1}": int(top[i] - offset) for i in range(3)},
                           **{f"likely_margin_{i + 1}_p": float(d[top[i]]) for i in range(3)}})
            others = seat_idx[:s] + seat_idx[s + 1:]     # by seat: two unknown judges share the average judge
            repl = [c for c in cand if at[c] not in seat_idx]
            pp = panel.panel_probs(card, np.array([[*others, at[c]] for c in repl]))
            for c, p in zip(repl, pp):
                swaps.append({"decision_id": did, "seat_rater_key": key, "seat_rater_name": rows_["rater_name"].iloc[0],
                              "replacement_rater_id": c, "p_a_wins": p[0], "p_draw": p[1], "p_b_wins": p[2]})
    F = pd.DataFrame(fights)
    names = judges.dropna(subset=["rater_id"]).drop_duplicates("rater_id").set_index("rater_id")["rater_name"].to_dict()
    W = pd.DataFrame(swaps)
    W["replacement_name"] = W["replacement_rater_id"].map({**names, AVERAGE: "Average judge", CROWD: "Crowd"})
    tables = {"FIGHTS": F, "CARDS": pd.DataFrame(cards_), "SWAPS": W}
    for t in tables.values():
        t.insert(0, "model_version", version)
    return tables


def scenario(a: dict, b: dict) -> dict:
    row = {f"a_{c}": 0.0 for c in STATS} | {f"b_{c}": 0.0 for c in STATS}
    row.update({f"a_{k}": v for k, v in a.items()})
    row.update({f"b_{k}": v for k, v in b.items()})
    return row


def strikes(landed: int, attempted: int) -> dict:
    head, body = round(landed * .6), round(landed * .2)
    return {"sig_str_landed": landed, "sig_str_attempted": attempted, "head_landed": head, "body_landed": body,
            "leg_landed": landed - head - body, "non_sig_str_landed": 5}


# What each judge rewards, on fixed rounds: A is out-struck 20-25 and leads control by
# 0-4 minutes (with a takedown); a dominant round; a control grind.
SCENARIOS = {**{f"p_a_wins_ctrl_{c // 60}min": scenario({**strikes(20, 50), "ctrl_seconds": c, "td_landed": int(c > 0), "td_attempted": int(c > 0)},
                                                       {**strikes(25, 55)}) for c in (0, 60, 120, 180, 240)},
             "p_10_8_dominant": scenario({**strikes(40, 70), "knockdowns": 1, "ctrl_seconds": 90, "td_landed": 1, "td_attempted": 1}, strikes(10, 40)),
             "p_10_8_grind": scenario({**strikes(12, 25), "ctrl_seconds": 270, "td_landed": 3, "td_attempted": 4, "ground_landed": 10}, strikes(6, 15))}


def judges_table(df: pd.DataFrame, basis, model, rounds: pd.DataFrame, version: str) -> pd.DataFrame:
    J = df[(df["rater_kind"] == "judge")].dropna(subset=["rater_id"])
    X = pd.DataFrame(list(SCENARIOS.values()))
    X["year"] = int(df["year"].max())
    ids = [*sorted(J["rater_id"].unique()), AVERAGE, CROWD]
    out = []
    Xt = basis.transform(X)
    k = rounds[rounds["rater_kind"] == "judge"].groupby("rater_id")
    for rid in ids:
        P = model.predict(Xt, np.full(len(X), rid, dtype=object), X["year"].to_numpy())
        row = {"rater_id": rid}
        for i, name in enumerate(SCENARIOS):
            row[name] = float(win_prob(P[i:i + 1])[0]) if name.startswith("p_a_wins") else float(P[i, 5:].sum())
        if rid in model.judges and model.rank:
            z = model.p["Z"][model.judges[rid]]
            row.update({f"style_{j + 1}": float(z[j]) for j in range(len(z))})
            row["offset_10_8"] = float(model.p["c"][model.judges[rid]])
        if rid not in (AVERAGE, CROWD):
            mine = J[J["rater_id"] == rid]
            row.update({"rater_name": mine["rater_name"].iloc[0], "rounds": len(mine), "named": bool(mine["named"].any()),
                        "first_year": int(mine["year"].min()), "last_year": int(mine["year"].max()),
                        "rate_10_8": float((mine["margin"].abs() >= 2).mean()), "rate_10_10": float((mine["margin"] == 0).mean()),
                        "rate_lone_dissent": float(mine["lone_dissent"].mean()),
                        "mean_p_call": float(k.get_group(rid)["p_call"].mean()), "mean_p_call_average": float(k.get_group(rid)["p_call_average"].mean())})
        else:
            row.update({"rater_name": "Average judge" if rid == AVERAGE else "Crowd", "named": False})
        out.append(row)
    T = pd.DataFrame(out)
    T.insert(0, "model_version", version)
    return T


def fighters_table(fights: pd.DataFrame, df: pd.DataFrame, version: str) -> pd.DataFrame:
    names = {**df.drop_duplicates("fighter_a_id").set_index("fighter_a_id")["fighter_a_name"].to_dict(),
             **df.drop_duplicates("fighter_b_id").set_index("fighter_b_id")["fighter_b_name"].to_dict()}
    side = lambda s, won: pd.DataFrame({"fighter_id": fights[f"fighter_{s}_id"], "won": (fights["result"] == won).astype(float),
                                        "expected": fights[f"average_panel_p_{s}_wins"]})
    L = pd.concat([side("a", "A"), side("b", "B")]).groupby("fighter_id").agg(decisions=("won", "size"), won=("won", "sum"),
                                                                                  expected=("expected", "sum")).reset_index()
    L["luck"] = L["won"] - L["expected"]
    L["fighter_name"] = L["fighter_id"].map(names)
    L.insert(0, "model_version", version)
    return L


def build(df: pd.DataFrame, basis, model, version: str) -> dict[str, pd.DataFrame]:
    rounds = rounds_table(df, basis, model, version)
    tables = {"ROUNDS": rounds, **fight_tables(df, basis, model, version)}
    tables["JUDGES"] = judges_table(df, basis, model, rounds, version)
    tables["FIGHTERS"] = fighters_table(tables["FIGHTS"], df, version)
    return tables


def write(session, tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Replace each ML.JUDGE_LENS_<name> whole."""
    for name, t in tables.items():
        t = t.copy()
        for c in ("event_date",):
            if c in t:
                t[c] = pd.to_datetime(t[c]).dt.date
        t.columns = [c.upper() for c in t.columns]
        session.write_pandas(t, f"JUDGE_LENS_{name}", schema="ML", auto_create_table=True, overwrite=True,
                             quote_identifiers=False, use_logical_type=True)
    return {name: len(t) for name, t in tables.items()}
