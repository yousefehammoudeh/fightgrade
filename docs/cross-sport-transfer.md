# Cross-sport transfer

> Related: `rating-model.md` (the definition this supplies `kappa` to) ·
> `promotion-strength-v2.md` (the same circularity rule, and the promotion strength this was
> tested against) · `fighter_style.md` (the matchup term this is *not*)

  kappa(a -> b)  =  rating points gained in sport b per rating point earned in sport a

`derive/cross_sport_transfer.py` -> `data/cross_sport_transfer.json`. ~40 s off the
cached bout table, no network.

    python3 derive/cross_sport_transfer.py
    python3 derive/cross_sport_transfer.py --validate

## Why it exists

The Elo engine currently multiplies K by `translation_to_mma`, one hand-set number per sport
in `dim_sport`, with a floor of 0.05. That column is the only thing standing between 303,225
non-MMA bouts and the rating system, and nothing derives it. It also cannot express what the
data plainly shows: transfer is **directional**. Grappling credentials carry into MMA more
than twice as well as MMA credentials carry into grappling.

Like promotion strength, this feeds the rating engine, so it may not be estimated from a
rating. Everything below comes from records and outcomes.

## The estimator

**Step 1 -- what a unit of record quality is worth inside a sport.** Bin every professional
bout by the quality gap `dq = q_own - q_opp`, where `q = 0.6*winrate + 0.4*experience` is the
same Laplace-smoothed measure `intake` and `elite` use. Take the empirical win rate per bin
and fit `logit(p)` against `dq` by weighted least squares; convert at `400/ln 10`.

```
sport                    bouts    logit/q    rating points per q  (c_s)
Boxing                 302,416       8.83                  1,533
MMA                    484,024       5.17                    898
Grappling               28,224       4.42                    768
Kickboxing              71,035       3.93                    683
Muay Thai               21,220       3.48                    605
Boxing (Bare Knuckle)   10,390       3.21                    558
```

Boxing's steepness is protected matchmaking: an unbeaten boxing record is built, and it
therefore carries far more information than an unbeaten MMA record.

**Step 2 -- what it is worth elsewhere.** For a fighter with at least 3 professional bouts in
`a` and at most 4 already in `b`, subtract the win probability their own quality gap *inside
b* already predicts, then regress that residual on their quality in `a` at the moment they
crossed. That gives `m(a->b)` in b's rating points per unit of a's quality, and

```
  kappa(a -> b)  =  m(a -> b) / c_a
```

which is exactly the propagation coefficient in the update rule below -- not a separate
quantity that has to be mapped onto one.

```
A -> B                                obs   pts in B / q_A   kappa
Boxing (Bare Knuckle) -> Boxing       431              540    0.97
Kickboxing -> Boxing (Bare Knuckle)   173              569    0.83
Boxing (Bare Knuckle) -> MMA          195              421    0.75
Muay Thai -> Kickboxing             1,435              311    0.51
MMA -> Muay Thai                      806              449    0.50
Kickboxing -> Muay Thai             1,957              330    0.48
Kickboxing -> Boxing                  798              307    0.45
Muay Thai -> Boxing                   147              263    0.44
Grappling -> MMA                      800              284    0.37
Boxing -> Kickboxing                  299              571    0.37
Boxing -> Boxing (Bare Knuckle)       761              519    0.34
MMA -> Boxing (Bare Knuckle)        2,492              291    0.32
MMA -> Boxing                       3,993              281    0.31
MMA -> Kickboxing                   2,780              278    0.31
Muay Thai -> MMA                      345              185    0.31
Kickboxing -> MMA                   1,599              191    0.28
Boxing -> MMA                         684              423    0.28
MMA -> Grappling                    2,726              122    0.14
Boxing -> Muay Thai                    84              177    0.12
```

**The scale conversion changes conclusions.** In raw win-rate terms Boxing -> MMA looks like
the strongest transfer in the dataset. As a fraction of what a boxing record is worth at home
it is among the weakest, because `c_Boxing` is 1,533 against MMA's 898. Anything comparing
sports has to divide through by the source sport's own scale.

The sharpest asymmetry is Grappling -> MMA at 0.37 against MMA -> Grappling at 0.14.

## What it is for

The rating is a per-sport vector with correlated components, not one number:

```
  R(f, s, t) = 1500 + G(f,t) + delta(f,s,t)      G general, delta ruleset specialisation
```

and after a bout in sport `s` with the usual Elo delta:

```
  dR(f, s)  = delta
  dR(f, s') = kappa(s -> s') * delta             every other sport
```

A fighter with bouts in one sport only never has another component updated, so this costs
nothing for the ~92% of the roster who never cross. For a debutant it also supplies a seed:

```
  R_seed(f, b) = 1500 + kappa(a -> b) * c_a * (q_a - qbar_a)
```

against the engine's present behaviour, which seeds every debutant at
`1500 + 150*(promotion_strength - 0.5)` -- 1500 to 1571 whoever they are.

## Three conditionings, all rejected

Each looked reasonable, each was tested out of sample, none earned its parameter.

**Champion credentials in the old sport.** Over the 80 crossover bouts carrying betting odds,
title-holders and non-title-holders are indistinguishable: our predicted win probability 33%
against 32%, actual outcomes 49% against 51%. Holding a belt in the sport you are leaving
says nothing extra about how you will do in the sport you are entering.

**Promotion strength at the debut card.** The hypothesis was selection -- a grappler the UFC
signs has been vetted, one on a regional card has not. In sample it looks real, and it flips
sign by direction: crossing *into* MMA transfer rises on strong cards (Grappling -> MMA 0.23
weak, 0.55 strong), crossing *out* of MMA into striking it falls (MMA -> Muay Thai 0.51 weak,
0.15 strong). Pooled across all pairs the effect is zero: 280 rating points per unit `q_A` on
weak cards against 290 on strong. Out of sample it is worth nothing (below).

**Percentile in the old sport instead of raw `q`.** `q` saturates at the top -- the failure
that forced the `elite` signal in promotion strength to be uncapped -- so a rank transform
should separate a world champion from a good regional fighter. It does not: Brier 0.2106
against 0.2104 for the flat form.

## Validation

Train on crossovers before 2019, test on everything after. The baseline is the destination
sport's own record telling you nothing about the fighter's other career.

```
model                          test obs    Brier   log loss   mean P
destination record only          18,846   0.2223     0.6351    46.2%
+ kappa                          15,863   0.2170     0.6229    47.5%
(actual win rate in test)                                      48.0%
```

Real and modest: a 2.4% cut in Brier and a 1.9% cut in log loss, and calibration close.
Earlier scoring against eventual record-implied ratings put it at an 8% reduction in seeding
error, concentrated in Muay Thai <-> Kickboxing (24%) and Grappling -> MMA (20%), with
MMA -> Boxing worth almost nothing.

**Betting odds are a benchmark, never the target.** A closing line carries style matchup, age,
layoff, camp and injury news, weight-cut reports and public bias on a famous name. Fitting
kappa to odds would be fitting it to all of that. It is fitted to outcomes; odds are read
against the *sum* of kappa, the style term `s(A,B)` and the age and layoff models, and
comparing kappa alone to a line is not a fair test of anything.

Worth knowing where the model and the market part company. On the general crossover
population the model is calibrated (47.5% predicted, 48.0% actual). On the 80 bouts that
carry a line it is about 13 points of probability too low, because the crossovers a promotion
builds a card around are a selected population -- Roger Gracie, Jacare, Ruotolo, Saki -- and
promotion strength, tested above, does not capture that selection.

## Limitations

1. **Thin pairs.** Boxing -> Muay Thai rests on 84 observations and BK Boxing -> Boxing on
   431 for a kappa of 0.97, which is implausibly close to full transfer and should be read as
   "these are nearly the same sport, with too little data to say more".
2. **q is a record, not an ability.** A fighter matched softly for twenty bouts and one
   matched hard for twenty carry the same `q`. Opponent quality is controlled inside the
   destination sport but only through the same measure.
3. **No time dependence.** kappa is a constant per pair. Whether transfer has changed as MMA
   professionalised is not tested, and the pre-2019 training split is the only evidence that
   it is stable at all.
4. **Selection at the top is unmodelled.** See the odds discussion above. The one systematic
   failure that survives is the elite crossover, and none of the three conditionings tried
   here reaches it.
5. **Amateur bouts are excluded** entirely, so a fighter's amateur career in another sport
   contributes nothing.

## Parameters

| | | |
|---|---|---|
| `MIN_A` | 3 | bouts in the source sport before it informs anything |
| `MAX_B` | 4 | bouts already had in the destination sport (near-debut only) |
| `MIN_FIT` | 3,000 | bouts before a sport gets its own within-sport curve |
| `MIN_PAIR` | 60 | observations before a pair gets its own kappa |
| `ALPHA` / `K_EXP` | 0.60 / 10 | inherited from promotion strength, so `q` means one thing |
| `CUT` | 2019-01-01 | train/test split for `--validate` |
