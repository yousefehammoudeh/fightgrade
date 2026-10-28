"""Fight-by-fight Elo for chosen fighters, outcome-only beside Judge Lens.

    python3 experiments/elo_mma/fighters.py            # the four showcase fighters
    python3 experiments/elo_mma/fighters.py jon-jones-bones

Reads the two replay logs engine.py writes. Prints and writes data/elo/fighters.csv.
"""
import sys
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parents[2] / "data" / "elo"
SHOWCASE = ["anderson-silva-the-spider", "40148-islam-makhachev", "jon-jones-bones", "charles-oliveira-do-bronx"]


def table(slug: str) -> pd.DataFrame:
    a = pd.read_parquet(DATA / "elo_log_nolens.parquet")
    a = a[a.fighter == slug]
    lens_path = DATA / "elo_log_lens.parquet"
    out = a[["date", "opponent", "result", "method", "strength", "opp_pre", "pre", "expected", "delta"]].rename(
        columns={"pre": "elo_before", "expected": "p_win", "delta": "change"})
    if lens_path.exists():
        b = pd.read_parquet(lens_path)
        b = b[b.fighter == slug].set_index("bout_id")
        out["lens_p_me"] = a["bout_id"].map(b["lens_p_me"]).values
        out["lens_elo_before"] = a["bout_id"].map(b["pre"]).values
        out["lens_change"] = a["bout_id"].map(b["delta"]).values
    out.insert(0, "fighter", slug)
    return out


def main():
    slugs = sys.argv[1:] or SHOWCASE
    frames = [table(s) for s in slugs]
    pd.set_option("display.width", 220, "display.max_rows", 500, "display.max_columns", 30)
    for f in frames:
        print(f"\n== {f.fighter.iloc[0]} ==")
        print(f.drop(columns="fighter").to_string(index=False, float_format=lambda v: f"{v:.2f}"))
    pd.concat(frames).to_csv(DATA / "fighters.csv", index=False)


if __name__ == "__main__":
    main()
