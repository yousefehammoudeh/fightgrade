"""Checks the model must pass before it is deployed (deploy.py runs them).

    gradients  the written-out gradient matches finite differences, for every
               parameter, on random rows with judges, unknown judges and the crowd
    mirror     swapping the fighters mirrors every probability exactly

    .venv/bin/python -m ml.judge_lens.checks
"""
import numpy as np

from .model import JudgeLens


def _toy(rng, n=400, k=6, judges=5):
    X = rng.standard_normal((n, k))
    Y = rng.dirichlet(np.ones(7) * 0.5, size=n)
    raters = np.array([None if i % 7 == 0 else f"j{i % judges}" for i in range(n)], dtype=object)
    years = rng.integers(2010, 2027, size=n)
    return X, Y, raters, years


def gradients(tol: float = 1e-6) -> float:
    """Largest relative gap between the analytic and the numerical gradient."""
    rng = np.random.default_rng(0)
    X, Y, raters, years = _toy(rng)
    m = JudgeLens(rank=2, era="three")
    m.fit(X, Y, raters, years, np.zeros(len(X), bool), np.ones(len(X)))
    p = {k: v + 0.1 * rng.standard_normal(v.shape) for k, v in m.p.items()}   # off the optimum
    idx, era = m.rater_index(raters), np.searchsorted([2017, 2022], years, side="right")
    crowd = (rng.random(len(X)) < 0.2).astype(float)
    w = rng.random(len(X)) + 0.5
    _, g = m._nll(p, X, Y, w, idx, era, crowd)
    worst, h = 0.0, 1e-6
    for key, val in p.items():
        for i in np.ndindex(val.shape):
            up, dn = {k: v.copy() for k, v in p.items()}, {k: v.copy() for k, v in p.items()}
            up[key][i] += h
            dn[key][i] -= h
            num = (m._nll(up, X, Y, w, idx, era, crowd, grad=False)[0]
                   - m._nll(dn, X, Y, w, idx, era, crowd, grad=False)[0]) / (2 * h)
            worst = max(worst, abs(num - g[key][i]) / max(1.0, abs(num)))
    assert worst < tol, f"gradient off by {worst:.2e}"
    return worst


def mirror() -> float:
    """Largest gap between P(fighters as given) and mirrored P(fighters swapped)."""
    rng = np.random.default_rng(1)
    X, Y, raters, years = _toy(rng)
    m = JudgeLens(rank=2).fit(X, Y, raters, years, np.zeros(len(X), bool), np.ones(len(X)))
    gap = float(np.abs(m.predict(X, raters, years) - m.predict(-X, raters, years)[:, ::-1]).max())
    assert gap < 1e-12, f"mirror off by {gap:.2e}"
    return gap


def run_all() -> dict:
    return {"gradients": gradients(), "mirror": mirror()}


if __name__ == "__main__":
    print(run_all())
