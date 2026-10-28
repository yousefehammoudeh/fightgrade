# Promotion strength, per sport and per date

> Related: `rating-model.md` (the definition this supplies `w_promotion` to) ·
> `promotion_strength.md` (v1, superseded -- the reasoning there still holds and is not
> repeated here) · `cross-sport-transfer.md` (what a rating in one sport is worth in another)
> · `belts.md` · `bout-title-stakes.md` · `weigh-ins.md`

  STRENGTH = f(promotion_id, sport, date, level) -> [0, 1],  0.5 = that era's median
                                                             professional promotion

`scrapers/derive/promotion_strength.py` -> `data/promotion_strength.jsonl`. ~2 min
(~2.5 with `--rebuild-cache`), no network.

    python3 scrapers/derive/promotion_strength.py
    python3 scrapers/derive/promotion_strength.py --export-ceiling
    python3 scrapers/derive/promotion_strength.py --at 61,"Muay Thai",2025-06-14   # exact
    python3 scrapers/derive/promotion_strength.py --query 61,"Muay Thai",2025-06-14 # gridded
    python3 scripts/validate_promotion_strength.py            # Major-Org separation
    python3 scripts/promotion_leaderboard.py --date 2026-08-01 --active-within 365

| output | rows | |
|---|---|---|
| `data/promotion_strength.jsonl` | 240,857 | quarterly grid: (promotion, sport, level, quarter). For browsing and analysis |
| `data/promotion_strength_events.jsonl` | 100,046 | **exact dates**: one row per date a promotion ran a card, window ending on that date. What a consumer joins on |
| `data/promotion_strength_field.json` | -- | field statistics, scaling and slopes, so `--at` can score any arbitrary date without a rerun |
| `data/overrides/promotion_strength.jsonl` | 3 | asserted strengths, hand-written (see **Overrides**) |

23 sports, 1993-2026 (152,207 professional rows, 88,650 amateur). `--export-ceiling` is off
by default; the shipped files were built with it on, and rows produced under it carry
`export` and `export_elite_produced`.

## Quarterly or exact?

**Exact.** Only the *field* statistics are gridded, and they are population aggregates over
a 3-year window that do not move inside a quarter. A promotion's own signals are computed
with the window ending on the date asked for -- the grid was never a limit on that, just on
where the normaliser is sampled.

Both are emitted because for most promotions it makes no difference and the quarterly table
is far easier to read: across all 97,981 event dates the two agree to a mean of 0.005, and
the UFC moves 0.9717 -> 0.9710 across half of 2016. Where it does matter:

* **1,672 event dates have no quarterly row at all.** At the quarter's first day the
  promotion's window held too little to score; by the date it actually ran, it did. On the
  grid those cards fall through to the 0.30 no-data default with data sitting right there.
* **Promotions that ramp inside a quarter.** Rajadamnern World Series on 2024-03-14 is 0.82
  exact against 0.51 on a grid point from before it had really started; World BARS
  Kickboxing in August 2006 is 0.76 against 0.41. p99 of the disagreement is 0.057 and the
  max is 0.35.

Exact dates are also strictly more causal: the window is `(date - 1095, date)` with the date
itself excluded, so a card on 20 December is no longer scored on data that stopped on
1 October.

## What changed from v1

v1 was one number per promotion for all time, MMA only, from 66,704 bouts. This is three
axes on 746,157 bouts across 101,130 events.

**Sport is part of the key.** ONE Championship is a top-3 MMA promotion, the largest Muay
Thai promotion on earth, a major kickboxing promotion and a serious grappling promotion,
and one number cannot say all four. It scores 0.93 / 0.95 / 0.90 / 0.89. v1's section 8
named this as its own headline limitation and said the fix needed the striking calendars
scraped; they are scraped -- ONE ran 401 MMA and 1,607 non-MMA bouts in 2023-25.

**Records stay, Elo stays banned.** `intake` and `matchmaking` are still built from what
the fighters' records were entering the bout (v1 sections 4.2, 5). The circularity
constraint (v1 section 1.1) is about Elo, not about records: nothing here reads a rating
the pipeline produces.

**Professional and amateur are separate tracks.** See below.

**Five signals added**, all from data v1 did not have: title integrity, card architecture,
discipline, distribution, and roster breadth.

## The ten signals

Window `W = (t - 1095 days, t)` for the quarter starting at `t`, professional bouts of that
sport only. `pro_share` is the exception -- it is measured over the whole calendar,
because a mostly-amateur calendar is the fact being measured.

| | |
|---|---|
| `intake` | mean over fighters *debuting with this promotion* in `W` of `0.6*winrate + 0.4*experience`, Laplace-smoothed, shrunk (m=25). Unchanged from v1 |
| `match` | mean record gap `abs(q_A - q_B)`, inverted, shrunk (m=50). **Only bouts where both men have >= 2 recorded fights** |
| `consistency` | `regularity * log(1 + events)`, `regularity = 1/(1 + CV(gaps between events))`, gaps including the trailing gap to `t` |
| `title` | `0.30*defence-share + 0.20*defenses-before + 0.15*reign-length + 0.15*(1 - belt inflation) + 0.20*title density`, shrunk (m=8). Density counts unlinked tournament finals; defence-share is dropped on a seasonal format |
| `card` | `0.35*pro_share + 0.25*championship-length share + 0.20*prelim structure + 0.20*card size` |
| `ops` | `1 - weight-miss rate`, from `bout_weigh_ins.over_lb`. Gated on >= 10 weighed bouts |
| `dist` | `0.40*broadcast tier + 0.15*bonus awards + 0.15*betting odds + 0.15*named referees + 0.15*published weigh-ins` |
| `elite` | mean over the **top decile** of the roster of `q_i = 0.6*winrate + 0.4*experience + 0.5*c_i`, where the champion credit `c_i = min(1, log1p(title bouts held)/log1p(5))`. The credit is capped; the score is not -- see below |
| `roster` | distinct professional fighters |
| `breadth` | distinct nationalities |
| `grad` | amateur track only: share of the promotion's fighters who later fight professionally |

Championship length is per sport: the 90th percentile of scheduled minutes in that sport,
so it is 5x5 in MMA and 12x3 in boxing without either being hardcoded.

**`elite` is the only signal that is not a mean or a count over the whole roster.** Everything
else averages, so a promotion whose strength is a small top-heavy roster disappears into its
undercard -- v1's limitation 3, and the one failure both external sources agree on: RIZIN is
our 12th and Fight Matrix's 2nd by average fighter rank. This reads the top decile instead,
and adds credit for having held an established belt (>= 5 bouts in its lineage, so a one-card
show inventing a title for its main event does not mint champions), scaled by title bouts
held so a five-defence champion outweighs a one-night winner. Causal: only reigns won before
`t` count.

It must be **uncapped** to work. The first version capped `q` at 1.0, and the top decile of
every credible promotion saturated: the signal separated majors from the field beautifully
(AUC 0.996) while scoring RIZIN 1.85, the UFC 1.86 and DWCS 1.84 -- no resolution at all
where it was needed. Uncapped it reads RIZIN third (2.39) behind the UFC (2.45) and KSW
(2.37) and ahead of ACA (1.85), LFA (1.91) and Cage Warriors (1.87), on a roster a third
their size, which is the behaviour it was added for.

**`elite` in full.** For promotion `p`, sport `s`, level `l`, at date `t`, over the bouts
`B` of `(p, s, l)` in the window and the fighters `F` appearing in them:

```
                   W + ½D + 1                              N
  ŵ(f,b)  =  ────────────────        e(f,b)  =  ───────────────      α = 0.60, κ = 10
                    N + 2                             N + κ

  q̃(f,b)  =  α·ŵ(f,b) + (1−α)·e(f,b)          W, L, D, N = W+L+D entering bout b

  C(f,s,t) =   Σ  (1 + δ_r)                    reigns r on established belts (≥ 5 bouts
             r: d_r < t                        in the lineage), won strictly before t;
                                               δ_r = defences in that reign

  γ(f,s,t) =  β · min( 1, ln(1 + C) / ln 6 )                          β = 0.50

  q(f)     =  max  q̃(f,b)  +  γ(f,s,t)         γ is b-independent, so the max commutes
             b ∈ B(f)

  m        =  min( |F|, max(5, ⌊|F|/10⌋) )     top decile, floored at five fighters

  elite    =  (1/m) · Σ_{i=1..m} q₍ᵢ₎          q₍₁₎ ≥ q₍₂₎ ≥ … , undefined if F = ∅
```

`ln 6 = ln(1+5)`, so five title bouts earns the full `β`. Only `γ`'s inner term is capped:
`q(f)` is deliberately unbounded, for the reason immediately below.

**`title` counts tournament finals, and does not charge for them.** A final is a title-level
bout with no incumbent. 2,032 of the 2,080 professional MMA tournament bouts carry no belt at
all -- `belts.py` cannot key the generic string "Tournament Championship", which is 2,040 of
them, to a division -- so they were invisible to this signal, and the PFL, whose season format
puts 66 of its 81 title-level bouts in that bucket, was scored on the 15 that remained. They
now count toward density, and where more than half a promotion's title bouts are tournaments
the defence share is dropped from the composite rather than scored: a final has nobody to
defend against, so a low share measures the format, not churn. Rows say which they are in
`title_format` (34,619 lineal, 3,011 seasonal in professional MMA). PFL's `title` z went
0.51 -> 0.95 and `z.title`'s own Major-Org AUC 0.9644 -> 0.9801.

The vacancy-periodicity test that would have been the more general fix is not available:
PFL's belts carry one or two reigns each, far too few to tell an annual season reset from
sporadic churn. The real repair is upstream, in `belts.py`, and is not attempted here.

**`consistency` measures predictability, not frequency.** The density form it replaced
(`months run / months spanned`) conflated running often with operating continuously, and
punished a deliberate low cadence: ADCC runs biennially like clockwork and scored as dormant
at #8 in grappling on 827 bouts. Gaps include the trailing gap to the window end, so a
promotion that ran three cards and vanished still reads as irregular. ADCC moved to #5,
0.804 -> 0.916, and `z.consistency` fell from 0.9756 to 0.9314 on its own Major-Org AUC --
correctly, because it is no longer a disguised volume proxy -- while the composite improved.

## Normalisation

Three steps, and the middle one is the one v1 did not need.

1. **Ratio to the field median** for that (sport, quarter). Field = promotions with >= 8
   fighters in `W`; carried forward when a quarter holds fewer than 3. Median, not mean:
   4,985 MMA promotions are mostly one-card shows and they drag a mean down until every
   real promotion sits at 3x it.
2. **z-score of `ln(ratio)`**, scaled on that sport's ratio distribution pooled over *all*
   quarters. Without this the weights are decorative: the seven signals have dynamic ranges
   from 0.8-1.7 (`intake`) to 1-7 (`consistency`), and an unstandardised weighted sum hands
   the widest-ranging signal the vote whatever the weights say. Pooling the scaling over
   time while the ratio itself stays era-relative is what preserves v1's cross-era stance --
   a 2005 major scores below a 2025 major because it sat lower in its own era's field.
3. **Logistic** on `quality + 0.35*scale`, slope set per sport so the 99.9th-percentile
   promotion-quarter reaches 0.95. The field median is at composite 0 -> exactly 0.5, which
   is the Elo code's neutral point. Sports with < 2,000 cells borrow the median slope of the
   large sports rather than calibrate a 99.9th percentile off 37 rows.

`quality` is the weighted mean of available z: intake .24, title .22, consistency .14,
card .14, match .12, dist .08, ops .06. `scale = 0.7*z(roster) + 0.3*z(breadth)` -- breadth
is what keeps a compact international promotion from being buried by a regional show that
runs 1,000 fighters a year, which was v1's section 4.6 tension.

**Availability gating.** `ops` and `dist` drop out and have their weight redistributed when
the promotion has no such data, because their absence is a fact about Tapology's US-facing
coverage, not about the promotion: odds cover 100% of UFC bouts, 13% of ACA's and 0% of
Shooto's. `title` is **not** gated above 4 events -- a belt is printed on the card, so
having none is a real deficit. Gating it was worth 0.22 of redistributed weight to Dana
White's Contender Series, which has no belts by design and signs undefeated prospects, and
that alone put it second in MMA behind the UFC.

**Cancellations and referees are attention, not reliability.** They sit in `dist`. The UFC
"cancelled" 525 bouts across 127 events in 2023-25 and Shooto 49 across 74; that is
Tapology tracking UFC bookings, and scoring it as unreliability inverted the signal.

**Sample size, in two dimensions.** The composite is shrunk toward the field median by
`bouts/(bouts + 10) * tenure_months/(tenure_months + 12)`. Bout count alone was not enough:
77 bouts across nine cards in a single year are not 77 independent observations, and UFC BJJ
was shrunk by only 0.89 and came top of grappling in its first year of existence (now #6,
0.941 -> 0.886). Tenure rather than event count, so a promotion that runs rarely on purpose
is not punished twice for the same cadence. Cells with fewer than 4 bouts at that level are
not emitted. Shrinking toward the median is also the right prior: what we do not know about a
promotion should read as average, not as excellent.

**Promotion identity.** The page's promotion block first, then the index attribution, then
the bare-text promoter from `event_promoter_freetext.jsonl` (`promoters-freetext.md`).
Free-text ids start at 1,000,001 and linked ones stop at 7,013, so they cannot collide. The
third fallback recovers 5,078 events and 29,837 bouts that were dropped whole -- among them
the IJF World Judo Championships, which carry no promotion id, no index id and no
`organisation`, only `production.promotion: "IJF"`.

**Sports too small to have a field of their own borrow a related one.** Judo and sambo
normalise against grappling, taekwondo against kickboxing, vale tudo against MMA, ice
fighting against bare-knuckle boxing. The fallback is conditional -- a sport with its own
field never uses it, and no scored sport moved when this was added -- and a row that used it
says so in `normalised_against`. Before it, those five sports produced no rows at all: the
field needs three promotions with >= 8 fighters in the same (sport, level, quarter) and they
have never had three at once, so every bout in them fell through to the 0.30 default.

## The amateur track

An amateur card is a lower-quality product than a professional one whatever its promoter
does well, and 307,898 of 820,880 bouts on file are amateur -- v1 recorded amateur bouts as
0.5% of its data and ignored them. So `level` is part of the key and each level is scored
separately. A promotion running both gets two rows and the consumer picks by the bout: LFA
is 0.94 professional and 0.40 amateur.

Three differences from the professional track:

* **Its own field.** An amateur promotion is normalised against amateur promotions of that
  sport and quarter, not against the professional field, so the signals measure what a good
  amateur programme looks like rather than how far short of the UFC it falls.
* **`graduation` replaces some of the weight.** The share of the promotion's amateur
  fighters who later have a professional bout -- an amateur programme's job is to produce
  professionals, and this is the only signal in the model that measures output rather than
  input. Weights: intake .18, **graduation .22**, consistency .14, card .12, match .12,
  title .12, dist .06, ops .06. `card` drops `pro_share` there (it is zero by construction
  inside an amateur slice) and renormalises the other three.
* **A band, with a permeable ceiling.** The amateur logistic is mapped onto `[0.15, ceil]`,
  so the median amateur programme lands on **0.30** -- the same constant the consumer already
  uses for "no data, assume below average". `ceil` is 0.45 by default, and nothing in the
  data fixes that exchange rate; it is asserted. Under `--export-ceiling` it rises toward
  0.90 with export evidence (below).

### The export ceiling

A flat 0.45 encodes "amateur is developmental". That is right for a regional amateur card and
flatly wrong for the Olympic Games, where amateur is the definitional status of the sport's
pinnacle: Olympic boxing ranks **first of 98** in its field on `intake` z = 5.0, and was
pinned at 0.435 by the ceiling alone.

So the ceiling rises with a measurement of what the programme produces:

```
ceil = 0.45 + 0.45 * min(1, rate/REF_rate) * min(1, log1p(elite)/log1p(REF_elite))
```

`rate` is the shrunk (m=50) share of the programme's fighters who had reached a professional
promotion scoring >= 0.85 strictly before `t`; `elite` is how many; both refs are the field's
99.9th percentile (0.102 and 88). Causal -- nothing after `t` is consulted -- and it needs
two passes, since the professional norms must exist before the amateur ones can be scored.
There is no circularity: professional strength never reads an amateur row.

**Both terms are load-bearing.** Rate alone ranked ten-fighter regional cards above the
Olympics, because "did our handful turn professional" rewards small feeders and penalises the
Games, where most Olympians never fight professionally at all. Scale alone would reward size.
Together they separate 55 elite professionals out of 1,132 Olympic boxers from 16 out of 102
at a good regional feeder. Olympic boxing goes 0.435 -> 0.611, and 0.744 in its strongest
window; the rest of the amateur world barely moves (median delta 0.0000, p90 +0.0085, and
only 400 of 88,650 amateur rows exceed 0.50).

**Fenced to the amateur track, on purpose.** Export cannot be a general quality term because
it inverts on terminal promotions -- DWCS exports at 77% against the UFC's 25%, which is v1's
rejected exchange centrality wearing a different hat. An amateur programme is never terminal,
so the inversion cannot bite here. Professional rows are bit-identical with the flag on and
off (max delta 0.000000).

Signals that describe the promotion rather than the fighters -- `consistency`, `card`,
`ops`, `dist`, and `pro_share` inside `card` -- are measured over the promotion's whole
calendar on both tracks. A promotion that also runs professional cards is a different animal
from one that never does, and the amateur row should say so.

Top of the MMA amateur track, Q4 2025: IMMAF .44, Fury FC .44, Peak Fighting .44, Synergy
.43. The International MMA Federation -- the actual world amateur governing body -- coming
first is the check that matters.

## Overrides

`data/overrides/promotion_strength.jsonl`, 3 rules. Every signal in this model is a volume
signal, and volume is exactly what an Olympic sport does not give us: we hold 24 Olympic judo
bouts and 69 Olympic wrestling bouts, present only because MMA fighters competed in them.
Scoring Olympic judo 0.317 does not say the Olympics is weak, it says there is no calendar to
measure. For a sport whose terminal competition IS the Games -- no professional tier exists
above it -- that is a fact about the sport rather than about the promotion, so it is asserted
rather than mined.

| promotion | sport | norm | why |
|---|---|---|---|
| IOC | Judo | 0.95 | Olympic judo is the sport's terminal competition |
| IOC | Wrestling | 0.95 | likewise; the professional wrestling promotions we hold recruit *from* it |
| IJF | Judo | 0.90 | World Judo Championships, one tier below the Games |

Deliberately **not** covered: Olympic boxing, because professional boxing sits above it and
the export ceiling already ranks it first among amateur boxing on evidence; and the six 2008
Beijing sanda bouts, which were a demonstration event, not a medal sport. There is no IOC
taekwondo in our data, so no rule was written for one.

An override replaces `norm` and records `source: "override"` alongside the `computed_norm` it
replaced. It also *creates* rows where nothing was computed -- 52 quarterly and 26 dated -- because
an Olympic final has no bouts in its own trailing window, the previous Games being four years
earlier, and the IJF's three world championships are one or two bouts each. Kayla Harrison's
ten judo bouts, two Olympic golds and two World Championships, scored 0.30 before this and
0.95 / 0.90 after.

## Causality

The window ends at `t` exclusive, whether `t` is a quarter boundary or an exact event date,
so a card can never contribute to the strength that weights it. Field statistics are read
from the quarter *containing* `t`, and those were themselves computed at the quarter's first
day -- earlier still. A promotion with no bouts in the window has no row, and the consumer's
default is 0.30 (v1's locked fallback, unchanged).

## What it produces

MMA, `norm` at Q4 of each year:

```
              1997   2001   2005   2007   2011   2015   2019   2022   2025
UFC           0.71   0.88   0.88   0.94   0.96   0.97   0.97   0.97   0.97
PRIDE            ·   0.68   0.85   0.88      ·      ·      ·      ·      ·
Strikeforce      ·      ·      ·   0.75   0.90   0.60      ·      ·      ·
Bellator         ·      ·      ·      ·   0.90   0.93   0.94   0.94   0.93
ONE              ·      ·      ·      ·   0.60   0.87   0.91   0.93   0.93
PFL              ·      ·      ·      ·      ·      ·   0.84   0.83   0.95
DWCS             ·      ·      ·      ·      ·      ·   0.82   0.84   0.85
Shooto        0.39   0.53   0.55   0.45   0.38   0.73   0.79   0.79   0.76
```

The dark-age UFC, PRIDE's rise to near-parity and its 2007 end, Strikeforce peaking in 2011
and gone by 2015, ONE's decade climb, PFL's step up on the Bellator acquisition.

Top of each sport, Q4 2025: **MMA** UFC .97, PFL .95, KSW .94, LFA .94, Oktagon .94,
Bellator .93, ONE .93. **Kickboxing** K-1 .96, Glory .95, RISE .95, ONE .91. **Muay Thai**
ONE .95, Rajadamnern World Series .93. **Grappling** ONE .90, FloGrappling .87, UFC Fight
Pass Invitational .85, Polaris .85. **Boxing** Top Rank .96, Matchroom .96, TGB .94,
Queensberry .94, Boxxer .93, Teiken .93.

## Validation

Two external sources in `data/rankings/`, built on different principles, checked by
`scripts/validate_promotion_strength.py`. Neither can be an input: Fight Matrix's orderings
are aggregates of its own fighter ratings, so using them would reintroduce exactly the
circularity v1 section 1.1 forbids.

**Tapology's Major-Org label** (read off the Major-Org event filter for MMA and Boxing,
2026-08-28) is an editorial tier: UFC, DWCS, PFL, ONE, LFA, ACA, RIZIN, Oktagon, KSW.
Bellator and Invicta are recorded `unknown_inactive` -- previously listed, no events now, so
a schedule cannot confirm them -- and are excluded from the test rather than scored as
negatives. Note what this corrected: an earlier reconstruction of this list had Bellator,
Cage Warriors and Invicta in and DWCS and Oktagon out, and every per-signal number computed
against it was wrong.

**Fight Matrix**, release #1038 (2026-08-23), four orderings of the same 13 promotions with
>= 100 ranked fighters: average fighter rank, total rating points, average rating points, and
count of ranked fighters.

MMA professional, 2026-08-23, 571 promotions active within a year, 9 labelled majors:

```
norm       AUC 0.9968      z.intake       0.9915     z.card         0.9315
quality        0.9968      z.match        0.9897     z.consistency  0.8921
scale          0.9862      z.elite        0.9881     z.dist  0.8430  z.ops  0.7885
                           z.title        0.8780

Fight Matrix Spearman   avg fighter rank 0.670   total points 0.780
                        avg points       0.677   ranked count 0.753
```

Eight of the nine majors are in our top seven: UFC 1, PFL 2, KSW 3, LFA 4, ACA 5,
ONE 6, Oktagon 7. The two misses:

* **RIZIN, our 12th, Fight Matrix's 2nd by average fighter rank.** Improved from 14th by the
  `elite` signal, which ranks it third on its own, but the composite still holds it down on
  scale and consistency: a 288-fighter roster running eight cards a year.
* **DWCS, our 24th.** A Major Org with no belts by design, few events and a feeder's roster.
  `title` scores it -3.9 sd and `consistency` little better. Rescuing it by reweighting would
  break the belt signal everywhere else.

Unlabelled promotions above rank 12 -- UAE Warriors, Cage Warriors, BRAVE, Cage Fury, ARES --
are credible second-tier internationals rather than noise, and Fight Matrix ranks four of
them in its own 13.

## Fitting the weights

`scripts/tune_promotion_strength.py`. The weights and `K_SCALE_Z` were priors and are now
fitted against both sources at once, with two guards, because 9 positives and 13 ranked
promotions cannot support a free fit:

* **Cross-source.** Fit on Fight Matrix, score on Major-Org, and the reverse. Both transfers
  beat the priors, so the gain is not purely curve-fitting.
* **Ensemble, not argmax.** The raw argmax puts `intake` at 0.001 -- the signal that on its
  own separates majors better than any other (AUC 0.9915) -- which is a corner, not a
  finding. The shipped weights are the mean of the top 1% of solutions.

Even so, across that top 1% `match` ranges 0.03-0.44 and `title` 0.00-0.45: the individual
weights are a direction, not a measurement. Weights and parameters also have to move
together -- applying the fitted weights while leaving `K_SCALE_Z`, `M_CELL` and `M_TENURE`
at their priors raised Major-Org AUC and *lowered* Fight Matrix agreement, 0.662 to 0.632.

```
                                  Major-Org AUC   Fight Matrix mean rho
priors (before fitting)                  0.9947                   0.662
fitted weights only                      0.9957                   0.632
fitted weights + fitted parameters       0.9957                   0.727
  + the `elite` signal at weight 0.12    0.9968                   0.720   <- shipped
```

**The two sources disagree about how much `elite` is worth**, and the disagreement is
informative rather than noise. Tapology's editorial tier rewards having notable fighters;
Fight Matrix's aggregates reward roster quality including size, since a point total scales
with headcount. Sweeping the weight:

```
elite weight   Major-Org AUC   FM rho   RIZIN rank   DWCS rank
       0.00          0.9957    0.727           14          25
       0.12          0.9966    0.720           13          21   <- shipped
       0.20          0.9974    0.677           12          18
       0.45          0.9984    0.677           10          15
```

Beyond 0.12 the Major-Org label keeps improving and Fight Matrix agreement falls off a cliff.
0.12 is the knee. Re-running the fit with `elite` in the search space does not beat the
shipped setting -- the ensemble of the top 1% scores 0.9941 / 0.723, worse on one criterion
and level on the other -- so the weights were left where they are.

A sanity check on the same search against a randomly drawn label of the same size reaches
AUC 0.63, against 0.996 on the real one.

## Which constants are measured

Most are not, and the ones that are should say so.

| | |
|---|---|
| `EXPORT_ELITE` | **measured.** Derived at run time as the 99th percentile of professional norms (0.86 on the current data, and it moves with each run). The hand-picked 0.85 sat at p98.85 of 152,155 cells, so this changes almost nothing -- but it is now a measurement that moves with the data instead of a number that goes stale |
| `FIELD_THIN` | **measured.** Quarter-to-quarter drift of the field median by field size: 0.0193 (< 10 promotions), 0.0099 (10-19), 0.0065 (20-29), **0.0042 (30-39)**, 0.0036 (40-49), then flat. 30 is the knee |
| `M_CELL`, `M_TENURE` | **not measurable from our own data.** Test-retest reliability of the composite at a one-year lag (119,690 pairs) rises monotonically with shrinkage -- 0.9139 at 10/12, 0.9348 at 40/36 -- because shrinking toward the median makes everything more stable. The criterion cannot tell stable-because-real from stable-because-large. They are fitted against the external rankings instead |
| quality weights, `K_SCALE_Z` | **fitted, weakly.** Against both sources, cross-source validated, ensembled over the top 1% of solutions -- see **Fitting the weights**. Poorly identified individually |
| `M_CELL`, `M_TENURE` (again) | **fitted jointly with the weights** to 14 / 7, but they do not survive as individual measurements -- they only matter in combination |
| `CHAMP_BONUS` (β), the belt floor of 5 bouts, the decile floor of 5 fighters | **asserted.** β = 0.5 sets how much a title reign is worth against a win record, and nothing in the data prices that exchange. The two floors are sample-size guards, not measurements |
| `w_elite` | **a prior, not fitted.** 0.120, chosen as the knee of the trade-off between the two sources rather than by the search -- refitting with `elite` in the space did not beat it. See **Fitting the weights** |
| `AM_FLOOR`, `AM_CEIL`, `AM_CEIL_MAX`, override values | **asserted, and no internal anchor exists.** Nothing in the data fixes the exchange rate between an amateur card and a professional one, or prices an Olympic final |

## Limitations

1. **The bottom is flat.** p10 = 0.46, p50 = 0.50, p90 = 0.62. Most promotions are genuinely
   indistinguishable and the sample-size shrink pulls small ones to the middle, so
   discrimination lives in the top decile. If Elo needs weak cards pushed harder toward the
   K floor, lower `M_CELL` or raise the slope -- do not confuse that with more information.
2. **`dist` is US-facing.** It is 0.08 of quality and gated, but it does read Anglophone
   broadcast and betting coverage as stature. ACA and ONE score 0.3-0.4 on it and are
   carried by the other six signals.
3. **Records outside MMA are reconstructed** from our own cards -- `record_after` exists for
   MMA only (96%) and nowhere else (0%). A boxer's early fights we never crawled are
   invisible, so `intake` is systematically low outside MMA. Per-sport normalisation absorbs
   the level of it; it does not absorb the noise.
4. **Sanctioning bodies score as promotions.** The WBC and the Japan Boxing Commission hold
   `promotion_id`s and appear in the boxing table. They are real rows about real events, but
   they are not promoters.
5. **16,028 events carry no `promotion_id`** and are dropped entirely (74,723 bouts). 5,265
   of them name a promoter only as free text (`promoters-freetext.md`).
6. **The amateur band is asserted, not measured.** `[0.15, 0.45]` encodes "amateur is worse
   than professional" as a decision, and `EXPORT_ELITE = 0.85` / `AM_CEIL_MAX = 0.90` are
   picks, not measurements. Nothing in the data sets the exchange rate.
7. **A dead promotion decays over three years rather than stopping.** Harmless for the Elo
   consumer, which only ever asks at an event date, but a leaderboard will show Bellator
   mid-table on 0 events in 365 days. `scripts/promotion_leaderboard.py` filters on
   `activity.events_last_365` and `activity.days_since_last_event` (`--active-within`), and
   excludes thin sports by default -- a sport with two promotions, or a field below
   `FIELD_THIN`, is never pooled with one ranked against 1,294.
8. **Five sport_bases still score nothing at all** -- Vale Tudo now scores, but Judo, Sambo,
   Taekwondo, Ice Fighting and the rest only where the kinship fallback or an override
   reaches them. Judo (52 bouts), Sambo (41), Taekwondo (14), Ice Fighting (37) are
   incidentally sampled: they appear because MMA fighters competed in them, so a computed
   number there measures our coverage, not the sport.
7. **Season-tournament belts read as churn.** PFL awards its title in a season final, so its
   `title` z is 0.5 against Bellator's 2.3. The format is detectable (`title_kind ==
   "tournament"`) and is not yet special-cased.

## Parameters

| | | |
|---|---|---|
| `WIN_DAYS` | 1095 | trailing window |
| `ALPHA` / `K_EXP` | 0.60 / 10 | intake win-rate weight / experience saturation |
| `CHAMP_BONUS` | 0.50 | elite: full credit for a champion with five title bouts behind them |
| `M_INTAKE` / `M_MATCH` / `M_TITLE` | 25 / 50 / 8 | shrinkage pseudo-counts |
| `MIN_REC` | 2 | fights on both records before a bout measures matchmaking |
| `MINF` | 8 | fighters in window to enter the field median |
| `K_SCALE_Z` | 0.42 | how far scale may move the composite, in field sd (fitted) |
| quality weights | match .214, card .201, title .152, elite .120, consistency .107, intake .099, dist .081, ops .027 | fitted, plus `elite` at a prior |
| `Z_CLIP` | 5.0 | per-signal cap |
| `M_CELL` / `M_TENURE` | 14 / 7 | bouts, then months of tenure, before a cell is trusted (fitted) |
| `AM_FLOOR` / `AM_CEIL` | 0.15 / 0.45 | the amateur band, median at 0.30 |
| `AM_CEIL_MAX` | 0.90 | highest ceiling the export evidence can buy |
| `EXPORT_ELITE` / `M_EXPORT` | p99 of pro norms (~0.86) / 50 | reaching the top; export shrinkage |
| `FIELD_THIN` | 30 | below this many field members the normalisation is flagged `field_thin` |
| `NO_DATA` | 0.30 | no bouts in window (consumer-side default) |
