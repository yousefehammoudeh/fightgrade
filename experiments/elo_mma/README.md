# MMA Elo with Judge Lens — spike and build plan

> Spike, 2026-10-28. Supersedes `DESIGN.md` §8 "Do NOT build the Elo engine" once the plan
> below lands. That section ruled the engine out because `docs/rating-model.md` asks for a
> per-sport rating vector with cross-sport transfer, which the one-rating engine cannot express.
> **MMA-only removes that requirement**: one sport, one rating, which is exactly what
> ufc-elo-calculator's System A already is.

```bash
python3 experiments/elo_mma/load.py          # pro MMA bouts from data/ -> data/elo/, ~10 s
python3 experiments/elo_mma/pull_lens.py     # Judge Lens fights from DEV (read-only)
python3 experiments/elo_mma/engine.py --no-lens && python3 experiments/elo_mma/engine.py   # ~3 s each
python3 experiments/elo_mma/fighters.py      # the four showcase fighters -> data/elo/fighters.csv
```

## What the spike is

- **Bouts:** every professional MMA bout in the local Tapology corpus with two linked fighters and
  a full date: **258,072** bouts, 129,359 fighters, 1992-03-26 .. 2026-08-21 (the corpus's own
  end). Bare-knuckle and amateur excluded. 84.4% carry a promotion strength; the rest take the
  0.30 no-data default.
- **Engine:** System A from `~/ufc-elo-calculator`, global track only, no cross-sport or division
  tracks. Kept: entry rating from promotion strength, K scaled by title rounds / finish /
  experience / layoff / promotion strength, decay toward 1500 after two years out, System A's
  method targets (KO 0.95, sub 0.92, UD 0.75, SD/MD 0.60), no update on draws and NCs.
- **Changed:** K0 24 → **200**, and Judge Lens in place of the hand-weighted Performance Score.

### Judge Lens as the decision score

For a UFC decision Judge Lens scored, the winner's target is

    S_winner = clip(0.5 + 0.40 * (2 * p_win - 1), 0.50, 0.90)

where `p_win` is `average_panel_p_*_wins`: the chance three average judges give the official
winner the fight from its round stats. The average panel, not the real one, so a judge's quirks
do not leak into the rating. A dominant decision earns 0.90, close to a finish; a decision the
stats give to the loser earns the winner a draw's credit (0.50).

4,002 of the 4,311 Judge Lens decisions map to a Tapology bout through `bout_id_map.csv`. The map
is from ufc-elo-calculator and stops in March 2026, so later UFC decisions fall back to method
targets.

### Backtest (in-sample: tuned on the years it scores; see the plan)

Log loss of the pre-fight expectation, decided bouts only; a coin flip is 0.6931.

| | bouts | log loss | accuracy |
|---|---|---|---|
| all pro MMA 2015+, K0 = 24 (System A) | 186,945 | 0.6748 | 59.8% |
| all pro MMA 2015+, K0 = 200 | 186,945 | 0.6287 | 65.4% |
| both fighters 5+ bouts, K0 = 200 | 52,386 | 0.6051 | 66.6% |
| **UFC 2012+, method targets** | 6,834 | 0.6628 | 60.0% |
| **UFC 2012+, Judge Lens targets** | 6,834 | **0.6577** | **60.5%** |

The K0 grid (24 … 400) bottoms out at 200–256. Judge Lens is a small, consistent gain on UFC
fights, best at spread 0.40 with a floor of 0.50 (0.35 floor: 0.6587). Over all MMA it barely
moves the number because it touches 4,002 of 258,072 bouts.

### The four fighters

Full tables: `data/elo/fighters.csv`. Where Judge Lens changes the story (change in rating,
method targets → Judge Lens targets; `p_win` is the average panel's chance for this fighter):

| fighter | fight | result | Judge Lens p_win | method | lens |
|---|---|---|---|---|---|
| Jon Jones | Dominick Reyes 2020 | W, UD | 0.35 | +2.9 | **−62.6** |
| Jon Jones | Alexander Gustafsson 2013 | W, UD | 0.52 | −16.9 | **−67.0** |
| Jon Jones | Rashad Evans 2012 | W, UD | 0.97 | +1.6 | **+37.3** |
| Jon Jones | Glover Teixeira 2014 | W, UD | 1.00 | −14.6 | **+27.9** |
| Anderson Silva | Derek Brunson 2017 | W, UD | 0.12 | +27.6 | **−18.2** |
| Anderson Silva | Daniel Cormier 2016 | L, UD | 0.00 | +8.2 | **−19.0** |
| Anderson Silva | Thales Leites 2009 | W, UD | 0.99 | −24.3 | **+14.2** |
| Islam Makhachev | Jack Della Maddalena 2025 | W, UD | 0.99 | −7.4 | **+17.3** |
| Islam Makhachev | Davi Ramos 2019 | W, UD | 0.99 | +33.1 | **+65.3** |
| Charles Oliveira | Arman Tsarukyan 2024 | L, SD | 0.10 | −59.4 | **−93.8** |
| Charles Oliveira | Michael Chandler 2024 | W, UD | 0.99 | −1.8 | **+28.7** |
| Charles Oliveira | Jeremy Stephens 2014 | W, UD | 0.36 | +50.3 | **+2.8** |

Two failures of method-only targets, both fixed: a clear decision win can *cost* a favourite
rating (Jones–Teixeira, Makhachev–JDM), and a clear decision loss to a better fighter can *gain*
rating (Silva–Cormier). And the two decisions the stats call gifts (Jones–Reyes, Silva–Brunson)
stop paying.

Peak ratings with Judge Lens: Makhachev 2,672 (after Della Maddalena; 2,650 after Machado Garry),
Oliveira 2,563 (before Makhachev), Jones 2,538 (after Miocic), Silva 2,396 (2012, before Bonnar).

### Caveats found along the way

- **The corpus is missing every bout that has no event page.** The crawl walked event pages
  to their bouts, and Tapology carries many fights, mostly regional and pre-2015, only as a bout
  on the fighters' records (Silva's Cage Rage 2004–06 fights show as a bare "Cage Rage", method
  "Win"). Measured by comparing each fighter's Tapology record (`record_after` on their last
  held bout) with the bouts we hold (`data/elo/record_gap.parquet`):

  | fighters | n | record complete | missing 1+ | missing 3+ | bouts missing |
  |---|---|---|---|---|---|
  | all | 126,011 | 74.6% | 23.3% | 11.5% | ~129,800 |
  | 5+ bouts held | 32,045 | 53.7% | 41.7% | 24.7% | ~75,800 |
  | ever in the UFC | 2,852 | 32.6% | 60.6% | 44.3% | ~12,700 |

  By the decade of a fighter's first held bout, 78% of 2000s fighters (5+ bouts) miss at
  least one, against 3% of 2020s fighters. Silva misses 12 (all bout-only: Cage Rage, Meca
  World Vale Tudo, Brazilian regionals), Oliveira 11, Jones 2, Makhachev 0. The loader drops
  only 1,713 pro MMA bouts (no result, or an unlinked fighter); the rest were never crawled.
  The event sitemap was complete (104,984 slugs).
- **The bout sitemap does not close it** (`experiments/bout_sitemap/`, fetched 2026-10-28, 254
  requests). It lists 966,764 bouts: 805,889 held, **160,875 not held**, 14,917 held but not
  listed. The not-held set is mostly cancelled bouts and other sports, and many bout-only
  records are not listed at all:

  | fighter | record gap | real gap bouts in the sitemap | the rest of their not-held entries |
  |---|---|---|---|
  | Jones | 2 | **2** (BCX 4, BCX 5) | 7 cancelled UFC bouts |
  | Silva | 12 | **7** (Cage Rage 14 and 16, Meca 1, 2, 4, 5, 6) | cancelled (UFC 36, 198, 212…), a 2026 exhibition |
  | Oliveira | 11 | **0** | BJJ, amateur, cancelled |
  | Makhachev | 0 | 0 | 10 cancelled UFC bouts |

  The diff above counts only the 966,880 entries whose slug starts with a bout id; 23,725 older
  entries have no id (`cage-rage-16-…-silva-vs-tony-the-freak-fryklund`) and are matched by slug
  in the table. So the sitemap reaches some bout-only records, misses others (Silva's other 5,
  which have no bout page at all, and all of Oliveira's 2008–10 regionals), and would spend most
  of a 160k fetch on cancelled bouts.
- **Favourites lose rating on modest wins.** System A's targets are below 1, so a 91% favourite
  who wins a 0.80 decision drops (Jones–St. Preux, −26). Correct under the model, odd on a page.
- **Injury stoppages are full KO losses** (Silva–Weidman 2, Oliveira–Holloway).
- `event_details` rows with `date_precision = 'assumed_year'` (1,225) are included; their year is
  a guess.

---

## Build plan

Deep dive Thu Nov 5. Each step lands as its own PR.

**1. Decide it in the spec (½ day).** Rewrite `DESIGN.md` §8 "Do NOT build the Elo engine" as
the MMA Elo decision and why scope changed it; Résumé Strength becomes Elo + résumé; update §10
non-goals and §11 phases. Note the cross-sport vector of `rating-model.md` stays designed, unbuilt.

**1b. Fill the bout-only gap — built** (`pipeline/tapology_fighters.py`, Dagster job
`tapology_fighter_crawl`, LANDING `lnd_tapology_fighter_records`). Smoke-tested in DEV on
2026-10-28: the fetched records hold exactly the census gap for Silva (12), Oliveira (11) and
Jones (2), every one with an opponent slug. The plan is **20,842 fighters** (Tapology record of
5+, any gap), ~117,700 missing bouts; at the 1,500-per-IP daily budget that is ~5 days on the
three IPs, ~8 h of crawling if the budget is lifted. Still to do: write the plan to PROD, launch
the three shards, then map `fighter_records` into `fct_bout` in step 2. Original reasoning:

Cheapest route:
the **fighter pages** of the ~13,400 fighters with 5+ held bouts and a gap. One page holds a
fighter's whole record (opponent, result, method, date, promotion), about 15 h at 4 s from one
IP. That's far less than fetching ~75,800 bout pages, and it is the only source that holds the
records the bout sitemap does not list (see the sitemap diff above). Missing rows land flagged `source = fighter_record`; both
fighters' sides are joined on the opponent link, and a row whose opponent has no slug
is kept for the record but does not update Elo. Rerun the census after: the gap for ranked
fighters should be ~0.

**2. Tapology bouts in the warehouse (2 days — the bulk).** REFINED: `dim_fighter`,
`dim_promotion`, `dim_event`, `fct_bout`, `fct_bout_participant` from the LANDING Tapology tables,
pro MMA scope applied in REFINED. Brings data-quality tests 1–3, 6, 8–10 (`DESIGN.md` §9) with
them. Also `xref_ufcstats_tapology_bout`, regenerated rather than copied from the stale CSV: the
same blocking-key match as MMADecisions (date + sorted surnames), match rate published.

**3. Promotion strength in the warehouse (1 day).** `promotion_strength` as a Dagster asset
before the dbt build, landing `promotion_strength_events` into LANDING. The rest of the derive
layer can follow later; Elo needs only this one.

**4. The engine (1 day).** `ml/elo/` from `experiments/elo_mma/engine.py`, run as
`ML.REPLAY_ELO` (Snowpark, like `ML.TRAIN_JUDGE_LENS`) at the end of `judge_lens_train`, since
it needs that run's Judge Lens fights. Writes `ML.ELO_HISTORY` (fighter × bout: pre, post,
expected, target, target source) and is deterministic: same inputs, same bytes.

**5. Tune and test honestly (1 day).** Tune K0, the lens spread and floor, entry scale and decay
on bouts up to 2019; test once on 2020–2024; from there a prospective log, as Judge Lens does.
Baselines: coin flip, win-rate, method-target Elo. Use only Judge Lens fights it did not train on
(`trained_on = false`, or the out-of-fold scores) for the test years, so the target is not fitted
to the same judges' cards.

**6. Published (1 day).** `pub_fighter_elo` (current rating, rank; ranked = 5+ pro MMA bouts and
active within two years, men and women separately), `pub_fighter_elo_history`,
`pub_fighter_resume` (each win scored by the opponent's Elo that night: best wins, median win,
share of wins over top-100 opponents), `pub_mma_strength`.

**7. Page (1 day).** A Rankings page beside Judge Lens and a fighter page: rating over time,
fight-by-fight table with method vs Judge Lens change, the résumé.

Cut order if it slips: the page's résumé panel → static table; division rankings (below) → not
built; step 3's remaining derive scripts → later.

### Open decisions

1. **Rank pound-for-pound only, or by division too?** System A kept a division track (K × 0.8).
2. **Gender:** separate rankings, using `fighter_population` gender.
3. **Favourite decision penalty:** keep System A's below-1 targets, or floor a win's change at 0.
4. **Injury stoppages:** full loss, or a reduced K.
5. **Ranking threshold:** 5 pro MMA bouts proposed.
