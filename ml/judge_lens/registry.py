"""The Model Registry entry ML.JUDGE_LENS: one version per fit (DESIGN.md §8).

A version is the fitted numbers (Basis.state + JudgeLens.state) as one JSON
artifact; predict() rebuilds the model from them with numpy. From SQL:

    select MODEL(ML.JUDGE_LENS)!predict(rater_id, year, a_knockdowns, ..., b_ctrl_seconds)

rater_id is a judge id, 'crowd', or 'average' (any id the version has not seen is
the average judge). The output is p_7_10 .. p_10_7 for fighter A, and p_a_wins.
Each version carries its holdout metrics, and train_through (the last event date
it trained on) in its comment's JSON, which the prediction log reads.
"""
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from snowflake.ml.model import custom_model

from .data import LABELS, STATS
from .features import Basis
from .metrics import win_prob
from .model import JudgeLens

MODEL = "JUDGE_LENS"
INPUTS = ["rater_id", "year", *[f"a_{c}" for c in STATS], *[f"b_{c}" for c in STATS]]
PIP = ["numpy==2.5.4", "scipy==1.18.1", "pandas==2.3.3"]
PYPI = {"pip": "snowflake.snowpark.pypi_shared_repository"}


class JudgeLensModel(custom_model.CustomModel):
    def __init__(self, context: custom_model.ModelContext) -> None:
        super().__init__(context)
        s = json.loads(Path(self.context.path("state")).read_text())
        self.basis, self.model = Basis.from_state(s["basis"]), JudgeLens.from_state(s["model"])

    @custom_model.inference_api
    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        X = X.rename(columns=str.lower)
        P = self.model.predict(self.basis.transform(X), X["rater_id"].to_numpy(object), X["year"].to_numpy(int))
        out = pd.DataFrame(P, columns=LABELS)
        out["p_a_wins"] = win_prob(P)
        return out


def package_dir() -> str:
    """judge_lens as a real directory, for code_paths: inside a stored procedure the
    package is imported from a zip, so it is copied out to /tmp first."""
    here = Path(__file__).resolve().parent
    if here.is_dir():
        return str(here)
    import zipfile
    archive, inner = str(here).split(".zip/", 1)
    out = Path(tempfile.mkdtemp())
    with zipfile.ZipFile(archive + ".zip") as z:
        z.extractall(out, [n for n in z.namelist() if n.startswith(inner.rstrip("/") + "/")])
    return str(out / inner)


def sample_input(df: pd.DataFrame) -> pd.DataFrame:
    s = df[["rater_id", "year", *INPUTS[2:]]].head(20).copy()
    s["rater_id"] = s["rater_id"].fillna("average").astype(str)
    s["year"] = s["year"].astype(np.int64)
    return s[INPUTS]


def log_version(session, basis: Basis, model: JudgeLens, sample: pd.DataFrame, version: str,
                metrics: dict, meta: dict, promote: bool):
    """Register a fit as a version of ML.JUDGE_LENS; make it the default if promote."""
    from snowflake.ml.registry import Registry

    reg = Registry(session=session, database_name=session.get_current_database().strip('"'), schema_name="ML")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        Path(path).write_text(json.dumps({"basis": basis.state(), "model": model.state()}))
        mv = reg.log_model(JudgeLensModel(custom_model.ModelContext(artifacts={"state": path})),
                           model_name=MODEL, version_name=version, comment=json.dumps(meta),
                           metrics={k: v for k, v in metrics.items() if v is not None and np.isfinite(v)},
                           pip_requirements=PIP, artifact_repository_map=PYPI, target_platforms=["WAREHOUSE"],
                           code_paths=[package_dir()], sample_input_data=sample)
    if promote:
        reg.get_model(MODEL).default = version
    return mv


def _registry(session):
    from snowflake.ml.registry import Registry
    return Registry(session=session, database_name=session.get_current_database().strip('"'), schema_name="ML")


def _model(session):
    reg = _registry(session)
    shown = reg.show_models()       # no columns at all before the first model
    names = set(shown["name"].str.upper()) if "name" in shown.columns else set()
    return reg.get_model(MODEL) if MODEL in names else None


def versions(session) -> set[str]:
    m = _model(session)
    return {v.version_name.upper() for v in m.versions()} if m else set()


def version(session, name: str):
    return _model(session).version(name)


def load(session, name: str):
    """(basis, model) of a registered version: its fitted numbers, in this code's
    classes, so a version registered before a change (panel.py's score()) still loads."""
    m = version(session, name).load(force=True)
    return Basis.from_state(m.basis.state()), JudgeLens.from_state(m.model.state())


def passed_versions(session) -> list[tuple[str, str]]:
    """(version, train_through) for every version that passed its gate, oldest first."""
    m = _model(session)
    out = []
    for v in (m.versions() if m else []):
        meta = json.loads(v.comment or "{}")
        if meta.get("train_through") and not meta.get("gate_failures"):
            out.append((v.version_name, meta["train_through"]))
    return sorted(out, key=lambda x: x[1])


def default_version(session):
    """(version name, its meta) for ML.JUDGE_LENS's default, or None before the first."""
    m = _model(session)
    if m is None:
        return None
    mv = m.default
    return mv.version_name, json.loads(mv.comment or "{}")
