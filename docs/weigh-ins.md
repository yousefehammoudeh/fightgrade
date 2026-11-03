# Per-fighter weights at the fight

**Purpose:** what each fighter actually scaled at, as opposed to what the bout was
contracted at.

`derive/weigh_ins.py` -> `data/bout_weigh_ins.jsonl`. ~40 s, no network — it reads
the archived bout pages in `data/html_bouts/`.

The contracted weight says a bout was booked at 125 lb. It does not say whether either man
made it. That is a different fact, it is on the page, and nothing in the pipeline read it
until now.

---

## 1. Three rows the bout page carries

The bout page has a fighter-vs-fighter comparison table laid out `left │ label │ right`.
Three of its rows are weights, and they mean different things:

| row | what it is |
|---|---|
| `Weigh-In Result` | what each fighter scaled at the official weigh-in |
| `Fight Day Weight` | what they walked to the cage at, after rehydrating |
| `Weight Gain` | the difference, in lb and % |

UFC 227 is the worked example — Cejudo vs Johnson, a 125 lb flyweight title:

```
Weigh-In Result    124.5 lbs (56.5 kgs)   125.0 lbs (56.7 kgs)
Fight Day Weight   141.0 lbs (64.0 kgs)   142.0 lbs (64.4 kgs)
Weight Gain         16.5 lbs (+13%)        17.0 lbs (+14%)
```

Both made the limit, then both put on ~13-14% overnight.

Each side is printed **twice** — a desktop cell and a mobile one — so the parser takes the
first and the last numeric value in the row, never the count.

---

## 2. Coverage, which is partial and matters

```
58,222  title bouts examined
22,012  carry a weigh-in          37.8%
36,162  page has no weight rows
    48  no cached page
    15  carry a fight-day weight  <- effectively UFC-only, do not build on it
```

Nothing is inferred where the row is absent. `Fight Day Weight` exists on 15 bouts in the
whole corpus; treat it as an anecdote, not a field.

---

## 3. What it is for

**It validates the contracted weight independently.** Across the 18,317 bouts carrying both:

```
80.0%  both fighters made the limit  (<= 0 lb over)
11.0%  within 1 lb
 7.3%  over by 1-5 lb
 1.4%  over by 5-20 lb
 0.3%  over by 20+   <- almost all uncapped super-heavyweight, where the badge is a floor
```

**91% within a pound.** That is the strongest evidence available that `weight_lb` on
`bout_title_stakes.jsonl` is trustworthy, and it comes from a field scraped independently
of the badge.

**It flags missed weight.** 1,960 title bouts have someone over the limit. `over_lb` gives
the margin per side; the threshold is 1 lb, because the customary 1 lb allowance on a
non-title bout does not apply to a title fight. This is a real rating signal that no other
field carries — a champion who misses weight is a different proposition from one who does
not, and `stake: champion_cannot_retain` only catches the 30 where Tapology says so
explicitly.

---

## 4. Row shape

```json
{"bout_slug": "371994-ufc-227-...", "contracted_lb": 125.0,
 "weigh_in_lb": [124.5, 125.0], "over_lb": [-0.5, 0.0],
 "fight_day_lb": [141.0, 142.0], "rehydration_lb": [16.5, 17.0]}
```

`over_lb` and `rehydration_lb` appear only when there is something to compute them from.
Sides are in the same order as `fighters` on the bout.

---

## 5. Known limits

- **Four weigh-ins are impossible** — `1248.0 lbs (566.1 kgs)` on a 125 lb bout. The raw
  HTML says exactly that, so it is Tapology's typo, not a parse error. They are left as
  found rather than silently corrected.
- **37.8% coverage is the ceiling for now.** The gap is pages that carry no weight rows at
  all, not pages we failed to crawl — HTML coverage of the ledger is 99.9%.
- **A weigh-in is per fighter, a contracted weight is per bout.** On a unification the
  contracted weight is shared across every belt row, but the weigh-in belongs to the bout.
