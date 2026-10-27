"""Cross-validation, the forward check and the locked test (DESIGN.md §8).

    mode 'cv'       five folds over period 'select' (2010-22), grouped by event
    mode 'forward'  train on 'select', score 'forward' (2023-24)
    mode 'test'     train on 'select' and 'forward', score 'test' (2025-26): once
    mode 'fit'      the retrain: CHOSEN on every usable row, gated on a backtest of
                    the newest HOLDOUT_DAYS, registered as a version of ML.JUDGE_LENS
                    and made the default if the gate passes
    mode 'log'      the prospective test: every usable round not logged yet, predicted
                    through the registry by the newest version that passed its gate
                    and never trained on it, appended to ML.JUDGE_LENS_PREDICTIONS

The training job runs 'log' before 'fit', so a new card is scored by the version
that existed before it. main() is the handler of the ML.TRAIN_JUDGE_LENS stored
procedure: cv/forward/test run one or more hyperparameter settings and log each
as a run in Snowflake Experiments. Locally, against DEV and without Experiments:

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
SE_METRICS = ["log_loss", "log_loss_named", "log_loss_10_8", "crowd_log_loss"]   # what each stage selects on

# The settings the protocol chose on 2026-10-27 (DESIGN.md §8); the retrain keeps
# them until the grids are run again.
CHOSEN = {"knots": 3, "interactions": "none", "era": "rules2017", "l2_beta": 10.0,
          "rank": 2, "m_z": 800.0, "m_108": 1600.0, "m_fan": 5.0, "seed": 0}
HOLDOUT_DAYS = 365          # the retrain's backtest: fit before these days, score them
GATE = {"max_calibration_error": 0.035,     # the locked test came out at 0.020
        "max_loss_over_average": 0.002}     # judge styles may not make it worse than the average judge
PREDICTIONS = "ML.JUDGE_LENS_PREDICTIONS"


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
    held, probs, avg, folds = [], [], [], []
    for train, test in splits(df, mode):
        basis, model = fit(train, p)
        P = predict(basis, model, test)
        held.append(test); probs.append(P); avg.append(predict(basis, model, test, as_average=True))
        folds.append(evaluate(P, test))
    held_df, P = pd.concat(held), np.vstack(probs)
    metrics = evaluate(P, held_df)
    if len(folds) > 1:
        # every setting is scored on the same folds, so the one-SE rule compares
        # settings fold by fold (select.py); the plain spread is kept for reference
        for key in SE_METRICS:
            vals = [f.get(key, float("nan")) for f in folds]
            metrics[f"{key}_fold_se"] = float(np.nanstd(vals, ddof=1) / np.sqrt(len(vals)))
            metrics.update({f"{key}_fold{k}": v for k, v in enumerate(vals)})
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


def registry_input(df: pd.DataFrame, as_average: bool = False) -> pd.DataFrame:
    """The registry model's input columns for these rows."""
    from .registry import INPUTS
    x = df.copy()
    x["rater_id"] = AVERAGE if as_average else pd.Series(raters(df), index=df.index).fillna(AVERAGE).astype(str)
    x["year"] = x["year"].astype(np.int64)
    return x[INPUTS]


def backtest(df: pd.DataFrame, p: dict) -> dict:
    """The gate's evidence: fit before the newest HOLDOUT_DAYS, score those days,
    beside the average judge from the same fit, with the fighter swap checked."""
    dates = pd.to_datetime(df["event_date"])
    end = dates.max()
    start = end - pd.Timedelta(days=HOLDOUT_DAYS)
    train, hold = df[dates < start], df[dates >= start]
    basis, model = fit(train, p)
    P, A = predict(basis, model, hold), predict(basis, model, hold, as_average=True)
    swapped = hold.copy()
    for c in data.STATS:
        swapped[f"a_{c}"], swapped[f"b_{c}"] = hold[f"b_{c}"].to_numpy(), hold[f"a_{c}"].to_numpy()
    mirror = float(np.abs(P - predict(basis, model, swapped)[:, ::-1]).max())
    return {"model": evaluate(P, hold), "average": evaluate(A, hold), "mirror": mirror,
            "holdout": [str(start.date()), str(end.date())]}


def gate(bt: dict) -> list[str]:
    """Why a fit may not become the default; empty when it may."""
    m, avg, failures = bt["model"], bt["average"], []
    if bt["mirror"] > 1e-9:
        failures.append(f"swapping the fighters does not mirror the probabilities ({bt['mirror']:.1e})")
    if m["calibration_error"] > GATE["max_calibration_error"]:
        failures.append(f"calibration error {m['calibration_error']:.4f} over {GATE['max_calibration_error']}")
    if m["log_loss"] > avg["log_loss"] + GATE["max_loss_over_average"]:
        failures.append(f"log loss {m['log_loss']:.4f} worse than the average judge's {avg['log_loss']:.4f}")
    return failures


def fit_and_register(session, df: pd.DataFrame, config: dict, exp) -> dict:
    """mode 'fit': backtest and gate, fit on everything, register, maybe promote."""
    from . import checks, registry

    checks.run_all()
    p = {**CHOSEN, **config.get("params", {})}
    if config.get("train_through"):     # a version as it would have been then: rehearses the prospective log
        df = df[pd.to_datetime(df["event_date"]) <= pd.Timestamp(config["train_through"])]
    through = str(pd.to_datetime(df["event_date"]).max().date())
    version = f"V{through.replace('-', '')}_{len(df)}"      # the data it saw: no new rows, no new version
    if version in registry.versions(session):
        return {"mode": "fit", "version": version, "registered": False, "reason": "already registered"}
    bt = backtest(df, p)
    failures = gate(bt)
    basis, model = fit(df, p)
    metrics = {**{f"holdout_{k}": v for k, v in bt["model"].items()},
               **{f"holdout_average_{k}": bt["average"][k] for k in ("log_loss", "calibration_error", "accuracy")}}
    meta = {"train_through": through, "rows": len(df), "params": p, "holdout": bt["holdout"],
            "gate_failures": failures}
    promote = bool(config.get("promote", True)) and not failures
    registry.log_version(session, basis, model, registry.sample_input(registry_input(df)), version,
                         metrics, meta, promote)
    exp.set_experiment("JUDGE_LENS_FIT")
    run = f"{version}_{int(time.time())}"      # a version can be fitted again after it is dropped
    exp.start_run(run)
    exp.log_params({**p, "rows": len(df), "train_through": through, "promoted": promote, "version": version})
    exp.log_metrics({k: v for k, v in metrics.items() if v is not None and np.isfinite(v)})
    exp.end_run(run)
    clean = {k: (v if v is None or np.isfinite(v) else None) for k, v in metrics.items()}
    return {"mode": "fit", "version": version, "registered": True, "promoted": promote,
            "gate_failures": failures, "train_through": through, "holdout": bt["holdout"], "metrics": clean}


def newest_unseen(event_dates: pd.Series, passed: list[tuple[str, str]]) -> pd.Series:
    """Per round, the newest gated version trained only on earlier events (None if
    there is none): a card is always scored by a version that never saw it, even
    when its decisions arrive after a newer version has trained."""
    dates = pd.to_datetime(event_dates)
    out = pd.Series([None] * len(dates), index=dates.index, dtype=object)
    for name, through in passed:            # oldest first, so the newest that fits wins
        out[dates > pd.Timestamp(through)] = name
    return out


def log_predictions(session, df: pd.DataFrame) -> dict:
    """mode 'log': every usable round not logged yet, predicted through the registry
    by the newest version that passed its gate and never trained on it."""
    from . import registry

    passed = registry.passed_versions(session)
    if not passed:
        return {"mode": "log", "logged": 0, "reason": "no version has passed its gate yet"}
    session.sql(f"""create table if not exists {PREDICTIONS} (
        model_version varchar, predicted_at timestamp_ltz, ufcstats_fight_id varchar, ufcstats_event_id varchar,
        event_date date, decision_id varchar, round int, rater_key varchar, rater_kind varchar, rater_id varchar,
        rater_name varchar, fighter_a_id varchar,
        {", ".join(f"{c} float" for c in LABELS)}, p_a_wins float, average_judge_p_a_wins float)""").collect()
    done = {tuple(r) for r in session.sql(f"select ufcstats_fight_id, round, rater_key from {PREDICTIONS}").collect()}
    new = df[[(f, int(r), k) not in done for f, r, k in zip(df["ufcstats_fight_id"], df["round"], df["rater_key"])]]
    new = new.assign(model_version=newest_unseen(new["event_date"], passed))
    new = new[new["model_version"].notna()]
    if new.empty:
        return {"mode": "log", "logged": 0, "newest": passed[-1][0]}
    logged = []
    for name, part in new.groupby("model_version"):
        mv = registry.version(session, name)
        out = mv.run(registry_input(part), function_name="predict")
        avg = mv.run(registry_input(part, as_average=True), function_name="predict")
        rows = part[["model_version", "ufcstats_fight_id", "ufcstats_event_id", "event_date", "decision_id", "round",
                     "rater_key", "rater_kind", "rater_id", "rater_name", "fighter_a_id"]].reset_index(drop=True)
        rows.insert(1, "predicted_at", pd.Timestamp.now(tz="UTC"))
        for c in [*LABELS, "p_a_wins"]:
            rows[c] = out[c.upper() if c.upper() in out.columns else c].to_numpy(float)
        rows["average_judge_p_a_wins"] = avg["P_A_WINS" if "P_A_WINS" in avg.columns else "p_a_wins"].to_numpy(float)
        rows["event_date"] = pd.to_datetime(rows["event_date"]).dt.date
        rows.columns = [c.upper() for c in rows.columns]
        session.write_pandas(rows, PREDICTIONS.split(".")[1], schema="ML", auto_create_table=False,
                             overwrite=False, use_logical_type=True)
        logged.append({"version": name, "rows": len(rows), "events": sorted(map(str, rows["EVENT_DATE"].unique()))})
    return {"mode": "log", "logged": sum(x["rows"] for x in logged), "by_version": logged}


def main(session, config: dict) -> dict:
    """ML.TRAIN_JUDGE_LENS(config): {"mode": ..., "experiment": ..., "configs": [params, ...]}.
    Each setting is a run in Snowflake Experiments in the current database's ML schema."""
    from snowflake.ml.experiment import ExperimentTracking

    mode = config.get("mode", "cv")
    settings = config.get("configs") or [config.get("params", {})]
    session.use_schema("ML")        # the registry's runs stage temporary tables in the current schema
    df = add_flags(data.from_snowpark(session))
    if mode == "log":
        return log_predictions(session, df)
    exp = ExperimentTracking(session=session, database_name=session.get_current_database().strip('"'),
                             schema_name="ML")
    if mode == "fit":
        return fit_and_register(session, df, config, exp)
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
