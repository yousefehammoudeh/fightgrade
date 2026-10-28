"""MMA-only Elo, ported from ufc-elo-calculator's System A, with Judge Lens scoring decisions.

    python3 experiments/elo_mma/engine.py                      # replay, report, the four fighters
    python3 experiments/elo_mma/engine.py --no-lens            # the same, outcome-only targets

One global rating per fighter, pro MMA only, so none of System A's cross-sport or division
tracks. What is kept from System A, with its tuned constants (K0 aside): K scaled by title rounds,
finish, experience, layoff and promotion strength; entry rating from promotion strength;
decay toward 1500 after two years out; method-based target scores (KO 0.95 ... SD 0.60).

What changes: System A nudged the target with its hand-weighted Performance Score. Here a UFC
decision that Judge Lens scored takes its target from the average panel instead:

    S_winner = clip(0.5 + LENS_SPREAD * (2 * p_win - 1), LENS_FLOOR, LENS_CEIL)

where p_win is the probability three average judges give the official winner the fight. A clear
decision earns nearly a finish's credit; a fight the stats call a robbery earns the official
winner a draw's credit, so a favourite who gets one loses rating. Finishes, and every decision
Judge Lens has no stats for, keep System A's targets.
"""
import argparse
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

DATA = Path(__file__).resolve().parents[2] / "data" / "elo"
BOUT_MAP = Path.home() / "ufc-elo-calculator/seeder_data/data/bout_id_map.csv"

BASE, SCALE = 1500.0, 400.0
K0 = 200.0      # System A's 24 was tuned on UFC alone; over all pro MMA a 2015+ grid bottoms out at ~200
TITLE_ROUNDS, FINISH_K = 1.15, 1.06
EARLY, VETERAN = 1.15, 0.92                 # < 5 fights, >= 20 fights
ACTIVE, LAYOFF, LAYOFF_DAYS = 1.03, 0.90, 540
DECAY_AFTER, DECAY_RATE, DECAY_MAX = 730, 0.03, 0.15
ENTRY_SCALE = 150.0                         # entry = 1500 + 150 * (strength - 0.5)
NO_STRENGTH = 0.30                          # promotion_strength's no-data default
TARGET = {"ko": 0.95, "sub": 0.92, "ud": 0.75, "md": 0.60, "sd": 0.60, "dq": 0.70, "other": 0.65}
LENS_SPREAD, LENS_FLOOR, LENS_CEIL = 0.40, 0.50, 0.90   # UFC 2012+ grid; a floor of 0.35 was 0.001 worse
MIN_FIGHTS = 5                              # ranked from this many pro MMA bouts


@dataclass
class Fighter:
    rating: float
    fights: int = 0
    last: pd.Timestamp | None = None
    history: list = field(default_factory=list)


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + math.pow(10.0, (rb - ra) / SCALE))


def k_factor(f: Fighter, bout, date) -> float:
    k = K0
    if (bout.scheduled_rounds or 0) >= 5 or bout.title:
        k *= TITLE_ROUNDS
    if bout.method_kind in ("ko", "sub"):
        k *= FINISH_K
    k *= EARLY if f.fights < 5 else (VETERAN if f.fights >= 20 else 1.0)
    k *= LAYOFF if f.last is not None and (date - f.last).days > LAYOFF_DAYS else ACTIVE
    s = bout.strength if not pd.isna(bout.strength) else NO_STRENGTH
    return k * min(max(s, 0.10), 1.0)


def decay(f: Fighter, date) -> None:
    if f.last is None:
        return
    days = (date - f.last).days
    if days > DECAY_AFTER:
        frac = min(DECAY_RATE * (days - DECAY_AFTER) / 365.25, DECAY_MAX)
        f.rating += frac * (BASE - f.rating)


def winner_target(bout, use_lens: bool) -> tuple[float, str]:
    if use_lens and bout.method_kind in ("ud", "sd", "md") and not pd.isna(bout.lens_p_winner):
        s = 0.5 + LENS_SPREAD * (2 * bout.lens_p_winner - 1)
        return min(max(s, LENS_FLOOR), LENS_CEIL), "lens"
    return TARGET[bout.method_kind], "method"


def load(use_lens: bool) -> pd.DataFrame:
    df = pd.read_parquet(DATA / "mma_pro_bouts.parquet")
    df["date"] = pd.to_datetime(df["date"])
    df["lens_p_winner"] = np.nan
    lens_path = DATA / "judge_lens_fights.parquet"
    if use_lens and lens_path.exists():
        lens = pd.read_parquet(lens_path)
        bmap = pd.read_csv(BOUT_MAP)
        lens = lens.merge(bmap, on="ufcstats_fight_id")
        # the average panel's chance of giving the official winner the fight
        lens["lens_p_winner"] = np.where(lens["result"] == "A", lens["average_panel_p_a_wins"],
                                         np.where(lens["result"] == "B", lens["average_panel_p_b_wins"], np.nan))
        p = lens.set_index("tapology_bout_id")["lens_p_winner"]
        df["lens_p_winner"] = df["bout_id"].map(p)
    return df


def replay(df: pd.DataFrame, use_lens: bool):
    fighters: dict[str, Fighter] = {}
    log = []
    for b in df.itertuples(index=False):
        s = b.strength if not pd.isna(b.strength) else NO_STRENGTH
        fa = fighters.setdefault(b.a_slug, Fighter(BASE + ENTRY_SCALE * (s - 0.5)))
        fb = fighters.setdefault(b.b_slug, Fighter(BASE + ENTRY_SCALE * (s - 0.5)))
        decay(fa, b.date)
        decay(fb, b.date)
        ea = expected(fa.rating, fb.rating)
        if b.result_a in ("D", "NC"):
            ta, source, ka, kb = 0.5, "draw" if b.result_a == "D" else "nc", 0.0, 0.0   # System A: no update
        else:
            tw, source = winner_target(b, use_lens)
            ta = tw if b.result_a == "W" else 1 - tw
            ka, kb = k_factor(fa, b, b.date), k_factor(fb, b, b.date)
        da, db = ka * (ta - ea), kb * ((1 - ta) - (1 - ea))
        for me, opp, slug, oslug, oname, e, t, d, res in (
            (fa, fb, b.a_slug, b.b_slug, b.b_name, ea, ta, da, b.result_a),
            (fb, fa, b.b_slug, b.a_slug, b.a_name, 1 - ea, 1 - ta, db, {"W": "L", "L": "W"}.get(b.result_a, b.result_a)),
        ):
            log.append((b.bout_id, b.date, slug, oslug, oname, res, b.method, b.method_kind, b.title,
                        b.strength, me.rating, opp.rating, e, t, d, source, b.lens_p_winner if res == "W" else
                        (1 - b.lens_p_winner if not pd.isna(b.lens_p_winner) else np.nan)))
        fa.rating += da
        fb.rating += db
        fa.fights += 1
        fb.fights += 1
        fa.last = fb.last = b.date
    cols = ["bout_id", "date", "fighter", "opponent_slug", "opponent", "result", "method", "method_kind", "title",
            "strength", "pre", "opp_pre", "expected", "target", "delta", "target_source", "lens_p_me"]
    return fighters, pd.DataFrame(log, columns=cols)


def backtest(log: pd.DataFrame, since="2015-01-01") -> dict:
    """Pre-fight expected vs result, one row per bout (side A), decided bouts only."""
    x = log[(log["date"] >= since) & log["result"].isin(["W", "L"])].drop_duplicates("bout_id")
    y = (x["result"] == "W").astype(float)
    p = x["expected"].clip(1e-6, 1 - 1e-6)
    ll = -(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()
    # ties in rating (two debutants) are coin flips
    acc = ((p > 0.5) == (y == 1)).where(p != 0.5).mean()
    return {"bouts": len(x), "log_loss": round(ll, 4), "accuracy": round(acc, 4), "coin_flip_ll": round(math.log(2), 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-lens", action="store_true")
    args = ap.parse_args()
    use_lens = not args.no_lens
    df = load(use_lens)
    fighters, log = replay(df, use_lens)
    tag = "lens" if use_lens else "nolens"
    log.to_parquet(DATA / f"elo_log_{tag}.parquet", index=False)
    print(f"{len(df):,} bouts, {len(fighters):,} fighters, lens targets on "
          f"{(log.target_source == 'lens').sum() // 2:,} decisions")
    print("backtest since 2015 (side A of each bout):", backtest(log))


if __name__ == "__main__":
    main()
