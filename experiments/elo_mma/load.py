"""Every professional MMA bout, one row per bout, for the Elo prototype.

    python3 experiments/elo_mma/load.py      # -> data/elo/mma_pro_bouts.parquet, ~2 min

Reads the local Tapology corpus (data/), not the warehouse. Dates and promotions come from
event_details on event_id (bouts.event_slug is stale, see docs/data-fixes.md), and each bout
gets its promotion's MMA pro strength on the most recent card date on or before the bout.
"""
import bisect
import json
import re
from pathlib import Path

import pandas as pd

DATA = Path(__file__).resolve().parents[2] / "data"
OUT = DATA / "elo" / "mma_pro_bouts.parquet"


def event_index() -> dict[int, tuple[str, int | None]]:
    """event_id -> (date, promotion_id) for completed events with a full date.

    events <-> event_details join on slug, with event_id as the fallback: some details rows
    carry no event_id (docs/field-census.md).
    """
    slug_of = {}
    with open(DATA / "events.jsonl") as f:
        for line in f:
            d = json.loads(line)
            slug_of[d["event_id"]] = d["event_slug"]
    by_slug, by_id = {}, {}
    with open(DATA / "event_details.jsonl") as f:
        for line in f:
            d = json.loads(line)
            if d.get("status") != "Completed" or not re.fullmatch(r"\d{4}-\d\d-\d\d", d.get("event_date_index") or ""):
                continue
            date = d.get("event_date_index")
            if not date:
                continue
            pid = d.get("index_promotion_id") or (d.get("promotion") or {}).get("promotion_id")
            row = (date, int(pid) if pid not in (None, "", "None") else None)
            if d.get("event_slug"):
                by_slug[d["event_slug"]] = row
            if d.get("event_id") not in (None, ""):
                by_id[int(d["event_id"])] = row
    out = {}
    for eid, slug in slug_of.items():
        row = by_slug.get(slug) or by_slug.get(f"{eid}-{slug}") or by_id.get(eid)
        if row:
            out[eid] = row
    return out


def strength_index() -> dict[int, tuple[list[str], list[float]]]:
    """promotion_id -> (sorted card dates, MMA pro strength on each)."""
    rows: dict[int, list[tuple[str, float]]] = {}
    with open(DATA / "promotion_strength_events.jsonl") as f:
        for line in f:
            d = json.loads(line)
            if d["sport"] == "MMA" and d["level"] == "pro" and d.get("norm") is not None:
                rows.setdefault(d["promotion_id"], []).append((d["date"], d["norm"]))
    return {p: ([x[0] for x in sorted(v)], [x[1] for x in sorted(v)]) for p, v in rows.items()}


def strength_on(index, promotion_id, date):
    if promotion_id not in index:
        return None
    dates, vals = index[promotion_id]
    i = bisect.bisect_right(dates, date) - 1
    return vals[i] if i >= 0 else None


def method_kind(method: str | None) -> str:
    m = (method or "").lower()
    if m.startswith("ko/tko") or m.startswith("ko") or m.startswith("tko"):
        return "ko"
    if m.startswith("submission"):
        return "sub"
    if m.startswith("decision"):
        if "split" in m:
            return "sd"
        if "majority" in m:
            return "md"
        return "ud"
    if "disqualification" in m or m.startswith("dq"):
        return "dq"
    return "other"


def main():
    events = event_index()
    strength = strength_index()
    rows = []
    with open(DATA / "bouts.jsonl") as f:
        for line in f:
            d = json.loads(line)
            if d.get("sport_base") != "MMA" or d.get("level") != "Professional":
                continue
            if d.get("sport") and "bare knuckle" in d["sport"].lower():
                continue
            ev = events.get(d.get("event_id"))
            fs = d.get("fighters") or []
            if ev is None or len(fs) != 2 or not all(x.get("fighter_slug") for x in fs):
                continue
            a, b = sorted(fs, key=lambda x: x["side"])
            if a["result"] not in ("W", "L", "D", "NC"):
                continue
            rounds = re.match(r"\s*(\d+)\s*x", d.get("round_format") or "")
            date, pid = ev
            rows.append({
                "bout_id": d["bout_id"], "event_id": d["event_id"], "date": date, "promotion_id": pid,
                "strength": strength_on(strength, pid, date),
                "a_slug": a["fighter_slug"], "a_name": a["name"], "b_slug": b["fighter_slug"], "b_name": b["name"],
                "result_a": a["result"], "method": d.get("method"), "method_kind": method_kind(d.get("method")),
                "duration": d.get("duration"), "scheduled_rounds": int(rounds.group(1)) if rounds else None,
                "title": d.get("title_kind") == "title", "order": d.get("order_effective"),
            })
    df = pd.DataFrame(rows).sort_values(["date", "event_id", "order"], ascending=[True, True, False])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, index=False)
    print(f"{len(df):,} pro MMA bouts, {df.date.min()} .. {df.date.max()}, "
          f"{df.strength.notna().mean():.1%} with a strength -> {OUT}")


if __name__ == "__main__":
    main()
