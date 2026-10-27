"""Cross-validation, the forward check and the locked test (DESIGN.md §8).

    mode 'cv'       five folds over period 'select' (2010-22), grouped by event
    mode 'forward'  train on 'select', score 'forward' (2023-24)
    mode 'test'     train on 'select' and 'forward', score 'test' (2025-26): once

main() is the handler of the ML.TRAIN_JUDGE_LENS stored procedure: it runs one
mode over one or more hyperparameter settings and logs each as a run in Snowflake
Experiments. Locally, against DEV and without Experiments:

    .venv/bin/python -m ml.judge_lens.train --mode cv --params '{"rank": 2}'
"""
import hashlib
import json
import time

import numpy as np
import pandas as pd

from . import data
from .data import LABELS
from .features import Basis
from .metrics import evaluate, win_prob
from .model import AVERAGE, CROWD, JudgeLens

DEFAULTS = {"knots": 0, "interactions": "gates", "era": "rules2017", "l2_beta": 1.0,
            "rank": 0, "m_z": 50.0, "m_108": 50.0, "m_fan": 50.0, "seed": 0}
TEST_FIGHTS = {"13677": "Pimblett def. Gordon", "13556": "O'Malley def. Yan", "15938": "Garbrandt def. Xiao Long"}
NAMED_MIN_ROUNDS = 300      # the named judges: this many usable rounds in 2010-24
FEW_ROUNDS = 100            # judges below this are reported apart


def add_flags(df: pd.DataFrame) -> pd.DataFrame:
    """named, few_rounds and lone_dissent, the subsets the metrics report."""
    df = df.copy()
    judge = df["rater_kind"] == "judge"
    counts = df[judge & (df["period"] != "test")].groupby("rater_id").size()
    df["named"] = judge & df["rater_id"].isin(counts[counts >= NAMED_MIN_ROUNDS].index)
    df["few_rounds"] = judge & ~df["rater_id"].isin(counts[counts >= FEW_ROUNDS].index)
    sign = np.sign(df["margin"].where(judge & (df["margin"] != 0)))
    key = [df["ufcstats_fight_id"], df["round"]]
    n, total = sign.groupby(key).transform("count"), sign.groupby(key).transform("sum")
    # three judges with a winner, two on one side: the one on the other side
    df["lone_dissent"] = (n == 3) & (total.abs() == 1) & (sign != np.sign(total))
    return df


def raters(df: pd.DataFrame) -> np.ndarray:
    return np.where(df["rater_kind"] == "crowd", CROWD, df["rater_id"].astype(object)).astype(object)


def fit(train: pd.DataFrame, p: dict) -> tuple[Basis, JudgeLens]:
    basis = Basis(p["knots"], p["interactions"]).fit(train[train["rater_kind"] == "judge"])
    model = JudgeLens(rank=p["rank"], m_z=p["m_z"], m_108=p["m_108"], m_fan=p["m_fan"], l2_beta=p["l2_beta"],
                      era=p["era"], seed=p["seed"])
    model.fit(basis.transform(train), train[LABELS].to_numpy(float), raters(train), train["year"].to_numpy(),
              (train["rater_kind"] == "crowd").to_numpy(), train["label_weight"].to_numpy(float))
    return basis, model


def predict(basis: Basis, model: JudgeLens, df: pd.DataFrame, as_average: bool = False) -> np.ndarray:
    who = np.full(len(df), AVERAGE, dtype=object) if as_average else raters(df)
    return model.predict(basis.transform(df), who, df["year"].to_numpy())


def splits(df: pd.DataFrame, mode: str):
    """(train, held-out) pairs for a mode."""
    if mode == "cv":
        sel = df[df["period"] == "select"]
        return [(sel[sel["cv_fold"] != k], sel[sel["cv_fold"] == k]) for k in range(5)]
    if mode == "forward":
        return [(df[df["period"] == "select"], df[df["period"] == "forward"])]
    if mode == "test":
        return [(df[df["period"] != "test"], df[df["period"] == "test"])]
    raise ValueError(f"mode must be cv, forward or test, not {mode!r}")


def run(df: pd.DataFrame, params: dict, mode: str) -> tuple[dict, pd.DataFrame]:
    """Held-out metrics for one setting, and the test fights it could score."""
    p = {**DEFAULTS, **params}
    held, probs, avg, fold_ll = [], [], [], []
    for train, test in splits(df, mode):
        basis, model = fit(train, p)
        P = predict(basis, model, test)
        held.append(test); probs.append(P); avg.append(predict(basis, model, test, as_average=True))
        fold_ll.append(evaluate(P, test)["log_loss"])
    held_df, P = pd.concat(held), np.vstack(probs)
    metrics = evaluate(P, held_df)
    if len(fold_ll) > 1:
        metrics["log_loss_fold_se"] = float(np.std(fold_ll, ddof=1) / np.sqrt(len(fold_ll)))
    metrics["n_params_per_judge"] = float(p["rank"] + 1)
    return metrics, test_fights(held_df, P, np.vstack(avg))


def test_fights(held: pd.DataFrame, P: np.ndarray, P_avg: np.ndarray) -> pd.DataFrame:
    """Every rater's call on the test fights this mode held out, beside the
    model's: P(A wins the round) for that rater and for the average judge."""
    mask = held["decision_id"].isin(TEST_FIGHTS).to_numpy()
    h = held[mask]
    actual_a = np.where(h["rater_kind"] == "judge", (h["margin"] > 0).astype(float),
                        h[LABELS[4:]].sum(axis=1) / (h[LABELS[:3]].sum(axis=1) + h[LABELS[4:]].sum(axis=1)))
    return pd.DataFrame({
        "fight": h["decision_id"].map(TEST_FIGHTS), "round": h["round"], "fighter_a": h["fighter_a_name"],
        "rater": h["rater_name"], "actual_p_a": actual_a.round(3),
        "pred_p_a": win_prob(P[mask]).round(3), "avg_judge_p_a": win_prob(P_avg[mask]).round(3),
        "pred_p_10_8": P[mask][:, [0, 1, 5, 6]].sum(axis=1).round(3),
    }).sort_values(["fight", "round", "rater"]).reset_index(drop=True)


def run_name(params: dict, mode: str) -> str:
    tag = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()[:8]
    return f"{mode}_r{params['rank']}_k{params['knots']}_{params['interactions']}_{tag}_{int(time.time())}"


def main(session, config: dict) -> dict:
    """ML.TRAIN_JUDGE_LENS(config): {"mode": ..., "experiment": ..., "configs": [params, ...]}.
    Each setting is a run in Snowflake Experiments in the current database's ML schema."""
    from snowflake.ml.experiment import ExperimentTracking

    mode = config.get("mode", "cv")
    settings = config.get("configs") or [config.get("params", {})]
    df = add_flags(data.from_snowpark(session))
    exp = ExperimentTracking(session=session, database_name=session.get_current_database().strip('"'),
                             schema_name="ML")
    exp.set_experiment(config.get("experiment", f"JUDGE_LENS_{mode.upper()}"))
    runs = []
    for params in settings:
        p = {**DEFAULTS, **params}
        metrics, fights = run(df, p, mode)
        name = run_name(p, mode)
        exp.start_run(name)
        exp.log_params({**p, "mode": mode, "rows": len(df)})
        exp.log_metrics({k: v for k, v in metrics.items() if np.isfinite(v)})
        path = f"/tmp/{name}_test_fights.csv"
        fights.to_csv(path, index=False)
        exp.log_artifact(path)
        exp.end_run(name)
        runs.append({"run": name, "params": p, "test_fights": fights.to_dict("records"),
                     "metrics": {k: (v if np.isfinite(v) else None) for k, v in metrics.items()}})   # VARIANT has no NaN
    return {"mode": mode, "rows": len(df), "runs": runs}


if __name__ == "__main__":
    import argparse
    import pickle
    from pathlib import Path

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="cv", choices=["cv", "forward", "test"])
    ap.add_argument("--params", default="{}", help="JSON: hyperparameters over DEFAULTS")
    ap.add_argument("--cache", type=Path, help="pickle the training rows here, and read them back next time")
    args = ap.parse_args()
    if args.cache and args.cache.exists():
        rows = pickle.loads(args.cache.read_bytes())
    else:
        from pipeline.tapology import snowflake_cursor
        cur = snowflake_cursor("dev")
        cur.execute("use database FIGHTGRADE_DEV")
        rows = data.from_cursor(cur)
        if args.cache:
            args.cache.write_bytes(pickle.dumps(rows))
    started = time.time()
    m, fights = run(add_flags(rows), json.loads(args.params), args.mode)
    print(json.dumps({k: round(v, 4) for k, v in m.items()}, indent=1))
    print(fights.to_string())
    print(f"{time.time() - started:.1f} s")
