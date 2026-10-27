"""The ordinal judge model (DESIGN.md §8).

A rater's score for fighter A in a round is

    eta = x . (beta + B z_j)            judges
    eta = x . (beta + delta)            the crowd
    eta = x . beta                      the average judge (z = 0), and unknown judges

x the round's shared terms (features.py), beta the population weights, B the
directions in which judges differ (columns of unit length), z_j a judge's style
vector. Seven ordered scores from A's side, 7-10 .. 10-7, through symmetric
cut-points +-t1 (10-10 | 10-9), +-t2 (10-9 | 10-8), +-t3 (10-8 | 10-7). t2 moves
with the era and with each rater's 10-8 offset c_j.

Two stages, as the design says: beta, B, z, the cut-points and the judges'
offsets are fitted on judge rows only, so beta stays the officials' standard;
then the crowd's delta and offset are fitted with the rest held fixed.

Shrinkage is in pseudo-rounds: z_j is pulled to zero as if the judge had M_z more
rounds scored like the average judge (the penalty is M_z times the information
one round carries, 0.25 for a winner call at even odds). c_j likewise with M_108
and the 10-8 rate's q(1 - q).

numpy and scipy only, with the gradients written out (tests check them against
finite differences): Snowflake installs the procedure's packages from PyPI,
whose torch brings gigabytes of CUDA libraries a model this size has no use for.
"""
import numpy as np
from scipy.optimize import minimize
from scipy.special import expit

ERAS = {"none": [], "rules2017": [2017], "three": [2017, 2022]}   # first year of each later bucket
AVERAGE, CROWD = "average", "crowd"
FLOOR = 1e-12


def era_index(years: np.ndarray, era: str) -> np.ndarray:
    return np.searchsorted(ERAS[era], years, side="right")


def softplus(x):
    return np.logaddexp(0, x)


class JudgeLens:
    def __init__(self, rank: int = 0, m_z: float = 50.0, m_108: float = 50.0, m_fan: float = 50.0,
                 l2_beta: float = 1.0, era: str = "rules2017", seed: int = 0, max_iter: int = 1000):
        self.rank, self.m_z, self.m_108, self.m_fan = rank, m_z, m_108, m_fan
        self.l2_beta, self.era, self.seed, self.max_iter = l2_beta, era, seed, max_iter
        self.judges: dict[str, int] = {}
        self.p: dict[str, np.ndarray] = {}

    # ------------------------------------------------------------- the model
    def _forward(self, p, X, idx, era, crowd):
        """eta, the cut-points and what the gradient needs, per row."""
        known = idx >= 0
        safe = np.where(known, idx, 0)
        eta = X @ p["beta"] + crowd * (X @ p["delta"])
        cache = {"known": known, "safe": safe}
        if self.rank:
            norm = np.linalg.norm(p["B_raw"], axis=0)
            B = p["B_raw"] / norm
            U, Zr = X @ B, p["Z"][safe] * known[:, None]
            eta = eta + (U * Zr).sum(axis=1)
            cache.update(norm=norm, B=B, U=U, Zr=Zr)
        e_full = np.concatenate([[0.0], p["e"]])
        off = e_full[era] + known * p["c"][safe] + crowd * p["c_crowd"][0]
        a0, a1, a2 = p["a"]
        t1 = softplus(a0)
        t2 = t1 + softplus(a1 + off)
        t3 = t2 + softplus(a2)
        cache.update(s1=expit(a0), s2=expit(a1 + off), s3=expit(a2))
        return eta, t1, t2, t3, cache

    @staticmethod
    def _probs(eta, t1, t2, t3):
        U = [expit(eta - t) for t in (t1, t2, t3)]
        D = [expit(-eta - t) for t in (t1, t2, t3)]
        P = np.stack([D[2], D[1] - D[2], D[0] - D[1], 1 - U[0] - D[0], U[0] - U[1], U[1] - U[2], U[2]], axis=1)
        return P, U, D

    def _nll(self, p, X, Y, w, idx, era, crowd, grad=True):
        """Weighted negative log likelihood of the soft labels, and its gradient."""
        eta, t1, t2, t3, c = self._forward(p, X, idx, era, crowd)
        P, U, D = self._probs(eta, t1, t2, t3)
        Pc = np.maximum(P, FLOOR)
        nll = -(w[:, None] * Y * np.log(Pc)).sum()
        if not grad:
            return nll, None
        g = -(w[:, None] * Y) / Pc                                   # dL/dp
        u = [x * (1 - x) for x in U]
        d = [x * (1 - x) for x in D]
        g_eta = (g[:, 0] * -d[2] + g[:, 1] * (d[2] - d[1]) + g[:, 2] * (d[1] - d[0]) + g[:, 3] * (d[0] - u[0])
                 + g[:, 4] * (u[0] - u[1]) + g[:, 5] * (u[1] - u[2]) + g[:, 6] * u[2])
        g_t1 = g[:, 2] * -d[0] + g[:, 3] * (u[0] + d[0]) + g[:, 4] * -u[0]
        g_t2 = g[:, 1] * -d[1] + g[:, 2] * d[1] + g[:, 4] * u[1] + g[:, 5] * -u[1]
        g_t3 = g[:, 0] * -d[2] + g[:, 1] * d[2] + g[:, 5] * u[2] + g[:, 6] * -u[2]
        G3 = g_t3
        G2 = g_t2 + G3                   # t3 = t2 + ...
        G1 = g_t1 + G2                   # t2 = t1 + ...
        g_off = G2 * c["s2"]
        known, safe = c["known"], c["safe"]
        out = {"beta": X.T @ g_eta, "delta": X.T @ (g_eta * crowd),
               "a": np.array([(G1 * c["s1"]).sum(), g_off.sum(), (G3 * c["s3"]).sum()]),
               "e": np.bincount(era, weights=g_off, minlength=len(p["e"]) + 1)[1:],
               "c": np.bincount(safe[known], weights=g_off[known], minlength=len(p["c"])),
               "c_crowd": np.array([(g_off * crowd).sum()])}
        if self.rank:
            gZ = np.zeros_like(p["Z"])
            np.add.at(gZ, safe[known], (g_eta[:, None] * c["U"])[known])
            gB = X.T @ (g_eta[:, None] * c["Zr"])
            B = c["B"]
            out["Z"] = gZ
            out["B_raw"] = (gB - B * (B * gB).sum(axis=0)) / c["norm"]
        return nll, out

    def _optimise(self, free: list[str], objective):
        """L-BFGS over the named parameters, the rest held fixed."""
        shapes = [self.p[k].shape for k in free]
        sizes = [int(np.prod(s)) for s in shapes]

        def unpack(v):
            parts = np.split(v, np.cumsum(sizes)[:-1])
            return {**self.p, **{k: part.reshape(s) for k, part, s in zip(free, parts, shapes)}}

        def f(v):
            loss, grads = objective(unpack(v))
            return loss, np.concatenate([grads[k].ravel() for k in free])

        res = minimize(f, np.concatenate([self.p[k].ravel() for k in free]), jac=True, method="L-BFGS-B",
                       options={"maxiter": self.max_iter})
        self.p = unpack(res.x)
        return float(res.fun)

    # --------------------------------------------------------------- fitting
    def fit(self, X: np.ndarray, Y: np.ndarray, raters: np.ndarray, years: np.ndarray,
            is_crowd: np.ndarray, label_weight: np.ndarray) -> "JudgeLens":
        X, Y = np.asarray(X, float), np.asarray(Y, float)
        rng = np.random.default_rng(self.seed)
        era = era_index(years, self.era)
        judge = ~is_crowd
        ids = sorted({r for r in raters[judge] if isinstance(r, str)})
        self.judges = {r: i for i, r in enumerate(ids)}
        idx = self.rater_index(raters)
        k, J, r = X.shape[1], len(ids), max(self.rank, 1)
        self.p = {"beta": np.zeros(k), "a": np.array([0.5, 3.0, 2.0]), "e": np.zeros(len(ERAS[self.era])),
                  "c": np.zeros(J), "B_raw": rng.standard_normal((k, r)), "Z": np.zeros((J, r)),
                  "delta": np.zeros(k), "c_crowd": np.zeros(1)}

        q = float((np.abs(Y[judge] @ np.arange(-3, 4)) >= 2).mean())
        Xj, Yj, ij, ej = X[judge], Y[judge], idx[judge], era[judge]
        wj, no_crowd, n_j = np.ones(len(Xj)), np.zeros(len(Xj)), len(Xj)

        def judge_objective(p):
            nll, g = self._nll(p, Xj, Yj, wj, ij, ej, no_crowd)
            k108 = self.m_108 * q * (1 - q)
            nll += 0.5 * self.l2_beta * (p["beta"] ** 2).sum() + 0.5 * k108 * (p["c"] ** 2).sum()
            g["beta"] = g["beta"] + self.l2_beta * p["beta"]
            g["c"] = g["c"] + k108 * p["c"]
            if self.rank:
                nll += 0.5 * self.m_z * 0.25 * (p["Z"] ** 2).sum()
                g["Z"] = g["Z"] + self.m_z * 0.25 * p["Z"]
            return nll / n_j, {key: val / n_j for key, val in g.items()}

        # the average judge first, then the style vectors from that start
        self.judge_nll_avg = self._optimise(["beta", "a", "e", "c"], judge_objective)
        self.judge_nll = (self._optimise(["beta", "a", "e", "c", "B_raw", "Z"], judge_objective)
                          if self.rank else self.judge_nll_avg)

        if is_crowd.any():
            Xc, Yc, ec = X[is_crowd], Y[is_crowd], era[is_crowd]
            n = np.asarray(label_weight, float)[is_crowd]
            wc = n / (n + self.m_fan)
            no_judge, ones = -np.ones(len(n), dtype=int), np.ones(len(n))

            def crowd_objective(p):
                nll, g = self._nll(p, Xc, Yc, wc, no_judge, ec, ones)
                nll += 0.5 * self.l2_beta * ((p["delta"] ** 2).sum() + (p["c_crowd"] ** 2).sum())
                g["delta"] = g["delta"] + self.l2_beta * p["delta"]
                g["c_crowd"] = g["c_crowd"] + self.l2_beta * p["c_crowd"]
                return nll / wc.sum(), {key: val / wc.sum() for key, val in g.items()}
            self.crowd_nll = self._optimise(["delta", "c_crowd"], crowd_objective)
        return self

    # ------------------------------------------------------------ predicting
    def rater_index(self, raters: np.ndarray) -> np.ndarray:
        return np.array([self.judges.get(r, -1) if isinstance(r, str) else -1 for r in raters], dtype=int)

    def predict(self, X: np.ndarray, raters: np.ndarray, years: np.ndarray) -> np.ndarray:
        """P(margin = -3 .. +3) per row. raters: a judge id, 'crowd', or anything
        else ('average', None, a judge never seen) for the average judge."""
        raters = np.asarray(raters, dtype=object)
        eta, t1, t2, t3, _ = self._forward(self.p, np.asarray(X, float), self.rater_index(raters),
                                           era_index(years, self.era), (raters == CROWD).astype(float))
        P = np.maximum(self._probs(eta, t1, t2, t3)[0], FLOOR)
        return P / P.sum(axis=1, keepdims=True)

    def state(self) -> dict:
        """Everything predict() needs, as plain lists: the registry's model and the
        fallback coefficients table are both built from this."""
        B = self.p["B_raw"] / np.linalg.norm(self.p["B_raw"], axis=0) if self.rank else 0 * self.p["B_raw"]
        return {"rank": self.rank, "era": self.era, "judges": list(self.judges), "B": B.tolist(),
                **{k: self.p[k].tolist() for k in ("beta", "a", "e", "Z", "c", "delta", "c_crowd")}}
