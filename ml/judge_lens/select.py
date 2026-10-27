"""The one-standard-error rule, paired by fold (DESIGN.md §8).

Every setting in a stage is scored on the same five folds, and the folds differ
from each other far more than the settings do (one fold's rounds are simply
harder). So a setting is measured against the best one fold by fold: it is
within one SE when its mean per-fold gap to the best is no more than the
standard error of that gap. Among those, the simplest wins: the fewest
parameters, then the strongest shrinkage, then the lowest metric.

    runs:   the "runs" list ML.TRAIN_JUDGE_LENS returns (params + metrics, with
            <metric>_fold0 .. _fold4)
"""
import numpy as np

INTERACTION_TERMS = {"none": 0, "gates": 3, "gates_td": 4}
ERA_PARAMS = {"none": 0, "rules2017": 1, "three": 2}


def complexity(p: dict) -> int:
    """Parameters the setting fits per judge-scored round's score, roughly: the
    population terms, rank more for each judge's style, and the era offsets."""
    terms = 11 + 6 * p["knots"] + INTERACTION_TERMS[p["interactions"]]
    return terms * (1 + p["rank"]) + ERA_PARAMS[p["era"]]


def shrinkage(p: dict) -> tuple:
    return (p["l2_beta"], p["m_z"] if p["rank"] else float("inf"), p["m_108"])


def folds(metrics: dict, metric: str) -> np.ndarray:
    return np.array([metrics[f"{metric}_fold{k}"] for k in range(5)], float)


def pick(runs: list[dict], metric: str = "log_loss") -> dict:
    """The winning run, with the comparison that chose it."""
    best = min(runs, key=lambda r: r["metrics"][metric])
    b = folds(best["metrics"], metric)
    scored = []
    for r in runs:
        gap = folds(r["metrics"], metric) - b
        se = gap.std(ddof=1) / np.sqrt(len(gap))
        scored.append({**r, "gap": float(gap.mean()), "gap_se": float(se), "within": bool(gap.mean() <= se)})
    under = [r for r in scored if r["within"]]
    won = sorted(under, key=lambda r: (complexity(r["params"]), tuple(-s for s in shrinkage(r["params"])),
                                       r["metrics"][metric]))[0]
    return {"pick": won, "best": best, "n_within": len(under), "scored": scored}
