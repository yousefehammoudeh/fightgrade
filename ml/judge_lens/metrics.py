"""What every run reports, on held-out rows only (DESIGN.md §8).

P is n x 7, P(margin = -3 .. +3) for fighter A. Judge rows are scored on their
hard call; crowd rows on the fans' distribution.
"""
import numpy as np
import pandas as pd

from .data import LABELS


def soft_log_loss(P: np.ndarray, Y: np.ndarray) -> np.ndarray:
    return -(Y * np.log(np.clip(P, 1e-12, 1))).sum(axis=1)


def win_prob(P: np.ndarray) -> np.ndarray:
    """P(A wins the round | someone does)."""
    a, b = P[:, 4:].sum(axis=1), P[:, :3].sum(axis=1)
    return a / (a + b)


def calibration_error(p: np.ndarray, y: np.ndarray, bins: int = 10) -> float:
    edges = np.minimum((p * bins).astype(int), bins - 1)
    return float(sum(abs(p[edges == b].mean() - y[edges == b].mean()) * (edges == b).mean()
                     for b in range(bins) if (edges == b).any()))


def evaluate(P: np.ndarray, df: pd.DataFrame) -> dict[str, float]:
    from sklearn.metrics import average_precision_score   # not at the top: the registry's model imports this module

    Y = df[LABELS].to_numpy(float)
    ll = soft_log_loss(P, Y)
    judge = (df["rater_kind"] == "judge").to_numpy()
    out = {"n_judge_rows": float(judge.sum()), "log_loss": float(ll[judge].mean())}
    for flag in ("named", "lone_dissent", "few_rounds"):
        mask = judge & df[flag].to_numpy(bool)
        if mask.any():
            out[f"log_loss_{flag}"] = float(ll[mask].mean())

    m = df["margin"].to_numpy(float)
    decided = judge & (m != 0)
    pa, won = win_prob(P)[decided], (m[decided] > 0).astype(float)
    out["brier"] = float(((pa - won) ** 2).mean())
    out["accuracy"] = float(((pa > 0.5) == (won == 1)).mean())
    out["calibration_error"] = calibration_error(pa, won)

    q = P[:, [0, 1, 5, 6]].sum(axis=1)[judge]
    big = (np.abs(m[judge]) >= 2).astype(float)
    out["pr_auc_10_8"] = float(average_precision_score(big, q)) if big.any() else float("nan")
    out["log_loss_10_8"] = float(-(big * np.log(q) + (1 - big) * np.log(1 - q)).mean())

    dsig = (df["a_sig_str_landed"] - df["b_sig_str_landed"]).to_numpy(float)[decided]
    called = dsig != 0
    out["baseline_sig_rule_accuracy"] = float(((dsig[called] > 0) == (won[called] == 1)).mean())

    if (~judge).any():
        out["crowd_log_loss"] = float(ll[~judge].mean())
    return out
