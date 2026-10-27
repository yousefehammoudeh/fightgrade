"""The shared scoring terms (DESIGN.md §8).

Every term is a difference (A - B), or a difference times something symmetric
(unchanged when the fighters swap). Swapping the fighters therefore negates
every term, and with no intercept and symmetric cut-points the model mirrors
exactly: no copy of each row in the other orientation is needed, because the
mirrored row's loss is the same number.

Terms are scaled by their root mean square on the training rows, never centred:
centring would break the mirror.
"""
import numpy as np
import pandas as pd

# term -> the fct_ufc_round_stats column it is the difference of
MAIN = {"kd": "knockdowns", "head": "head_landed", "body": "body_landed", "leg": "leg_landed",
        "sig_att": "sig_str_attempted", "non_sig": "non_sig_str_landed", "td": "td_landed",
        "td_att": "td_attempted", "ctrl_min": "ctrl_seconds", "sub": "sub_attempts", "rev": "reversals"}
# diminishing returns: hinge terms at knots on these
SPLINED = ["head", "body", "leg", "sig_att", "non_sig", "ctrl_min"]
INTERACTIONS = {"none": [], "gates": ["ctrl_x_sub", "ctrl_x_ground", "sig_x_no_kd"],
                "gates_td": ["ctrl_x_sub", "ctrl_x_ground", "sig_x_no_kd", "td_x_ctrl"]}


def diffs(df: pd.DataFrame) -> dict[str, np.ndarray]:
    d = {t: (df[f"a_{c}"] - df[f"b_{c}"]).to_numpy(float) for t, c in MAIN.items()}
    d["ctrl_min"] = d["ctrl_min"] / 60
    return d


def interaction(name: str, df: pd.DataFrame, d: dict) -> np.ndarray:
    both = lambda c: (df[f"a_{c}"] + df[f"b_{c}"]).to_numpy(float)
    if name == "ctrl_x_sub":        # control that comes with a submission attempt
        return d["ctrl_min"] * (both("sub_attempts") > 0)
    if name == "ctrl_x_ground":     # active control: ground strikes landed by either fighter
        return d["ctrl_min"] * both("ground_landed") / 10
    if name == "sig_x_no_kd":       # striking in a round with no knockdown
        return (df["a_sig_str_landed"] - df["b_sig_str_landed"]).to_numpy(float) * (both("knockdowns") == 0)
    if name == "td_x_ctrl":         # takedowns that led to control
        return d["td"] * both("ctrl_seconds") / 60
    raise ValueError(name)


class Basis:
    """knots: hinge terms per splined difference (0 = linear); interactions: a
    key of INTERACTIONS. fit() on training rows sets the knots and scales."""

    def __init__(self, knots: int = 0, interactions: str = "gates"):
        self.knots, self.interactions = knots, interactions
        self.knot_at: dict[str, list[float]] = {}
        self.scale: np.ndarray | None = None
        self.names: list[str] = []

    def _raw(self, df: pd.DataFrame) -> np.ndarray:
        d, cols, names = diffs(df), [], []
        for t in MAIN:
            cols.append(d[t]); names.append(t)
            for i, k in enumerate(self.knot_at.get(t, [])):
                cols.append(np.sign(d[t]) * np.maximum(np.abs(d[t]) - k, 0)); names.append(f"{t}_h{i + 1}")
        for name in INTERACTIONS[self.interactions]:
            cols.append(interaction(name, df, d)); names.append(name)
        self.names = names
        return np.column_stack(cols)

    def fit(self, df: pd.DataFrame) -> "Basis":
        d = diffs(df)
        self.knot_at = {}
        if self.knots:
            for t in SPLINED:
                a = np.abs(d[t][d[t] != 0])
                qs = np.quantile(a, [(i + 1) / (self.knots + 1) for i in range(self.knots)])
                self.knot_at[t] = sorted(set(float(q) for q in qs))
        x = self._raw(df)
        rms = np.sqrt((x ** 2).mean(axis=0))
        self.scale = np.where(rms > 0, rms, 1.0)
        return self

    def transform(self, df: pd.DataFrame) -> np.ndarray:
        return self._raw(df) / self.scale

    def state(self) -> dict:
        return {"knots": self.knots, "interactions": self.interactions, "knot_at": self.knot_at,
                "scale": self.scale.tolist(), "names": self.names}

    @classmethod
    def from_state(cls, s: dict) -> "Basis":
        b = cls(s["knots"], s["interactions"])
        b.knot_at, b.scale, b.names = s["knot_at"], np.asarray(s["scale"], float), s["names"]
        return b
