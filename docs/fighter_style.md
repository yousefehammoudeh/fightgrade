# Fighter Style: A Vector Model and a Level-Neutral Matchup Edge

**Status:** Locked (v1)
**Scope:** UFC, men's divisions (women's pool fit separately; see §8)
**Consumed by:** a **matchup-adjustment layer** on top of Elo (see §6) — the style edge
`s(A,B)` is designed to be added to an Elo difference, not used alone. Not yet wired
into `system_a_elo_ps`.
**Code:** `context/analysis/{build_features,cluster,style_matchup,style_edge,seed_test}.py`

---

## Abstract

We quantify a fighter's *style* as a mathematical object and reason about *matchups* —
"styles make fights" — the way `promotion_strength` quantifies promotion quality. This
document defines: (1) a **behavioural style vector** per fighter, built only from what
happens in the cage; (2) interpretable **capability composites** covering **all 56**
profile features; (3) an **asymmetric matchup operator** in which A's offense meets B's
defense, phase by phase; and (4) the central object — a **level-neutral style edge**
`s(A,B) = β_style · x_style`, the log-odds that A's *style* beats B's *style* between
equal-quality fighters. Overall quality (`win_rate`, `LEVEL`) is deliberately **removed**
— that is Elo's job — so the full prediction is `logit P = a·(Elo_A − Elo_B) + s(A,B)`.

Two load-bearing results: **pure style predicts outcomes at CV AUC 0.66**, about the
same as overall level alone — "styles make fights" is real and measured — and the edge
**decomposes** into named clashes, so every verdict is a readable sum, not a black box.

---

## 1. Motivation

### 1.1 Style is a continuum, not a label
Nested clustering (`cluster.py`) showed MMA style does **not** fall into cleanly
separated clusters: silhouette is low at every `k`, and the elite grapplers (Khabib,
Islam, Chimaev) never separate from each other or from GSP/Hughes even at 20 sub-styles.
**Hard archetype labels are the wrong primitive.** The right primitive is a **continuous
vector** with a **matchup geometry** on it.

### 1.2 A matchup is directional
Distance in style space says *nothing* about who wins — a striker and grappler are
maximally far apart, but the fight turns on whether the grappler's offense beats the
striker's defense. The operator must be **asymmetric**, `M(A,B) ≠ M(B,A)`. That is the
whole content of "styles make fights."

### 1.3 The circularity discipline (from promotion strength)
As promotion strength must be Elo-independent, the **style vector must be
outcome-independent**: built from *behaviour* (strikes, takedowns, control, position,
pace), never from win/loss record or a rating. Outcomes calibrate the operator; they
never define a fighter's style. Consequently `win_rate` and overall `LEVEL` are treated
as **quality**, not style, and are excluded from the style edge (§5.4).

---

## 2. Data

Style vectors are the career profiles from `cluster.py`, over **all-time UFC bouts with
complete stats** (10,765 fights, 1993–2025; both fighters have full totals + positional
+ target breakdowns). The operator is calibrated on **4,418 decisive men's bouts** where
both fighters have **≥ 8 career fights** (`MIN_FIGHTS`, for stable vectors); base rate
after random A/B assignment is **0.489**.

---

## 3. Design goals

1. **Behavioural, outcome-independent** style vector (§1.3).
2. **Continuous** geometry (§1.1) — supports matchup scoring *and* "who resembles whom".
3. **Asymmetric** operator (§1.2).
4. **Interpretable**: every edge is a sum of named clash contributions.
5. **Level-neutral**: quality is removed so the edge is a pure style term to add to Elo.
6. **Calibrated & honestly benchmarked** (AUC/log-loss vs a coin flip).

---

## 4. Methodology: how we got here

### 4.1 Hard clusters + empirical matchup matrix (rejected as the mechanism)
The cluster-vs-cluster win matrix (`matchup.py`) is fine *descriptively* but useless as
the primitive: styles don't cluster cleanly (§1.1), cells are sparse, and it can't place
a specific pair. Kept for validation only.

### 4.2 Symmetric style distance (rejected as a predictor)
A kernel `K(f,g)=exp(−d²/2σ²)` is useful for *precedent* ("who has f faced that
resembles o?") and we use it that way — but as a matchup predictor it is wrong (§1.2):
distance is symmetric; it cannot say who wins.

### 4.3 Level-collinear clashes (rejected — this one bit us)
Built from raw capabilities, the clash terms are collinear with overall level (their sum
≈ the level difference). Fit directly, one LEVEL term absorbed everything (+0.98) and the
style clashes came out tiny and wrong-signed. The style analogue of promotion strength's
"connectivity is not quality": *remove the dominating quantity before measuring the one
you care about.*

### 4.4 The fix: level/shape orthogonalization (locked)
Split each fighter's capabilities into **level** (mean of the capability composites) and
**shape** (each composite minus that fighter's own level). Level = "how good"; shape =
"good at *what*, relatively." Style clashes use **shapes**; quality is carried only by
LEVEL/`win_rate`, which the style edge then drops. After this, style stands on its own
(§7).

### 4.5 `win_rate` is quality, not style (rejected as a style feature)
Adding `win_rate` lifts raw AUC to 0.775 — but `win_rate` **alone** scores 0.765: it is
nearly the label. Including it as "style" is the §1.3 circularity. It is therefore
quarantined to the **skill-prior slot** (the Elo stand-in, §6) and excluded from
`s(A,B)`.

---

## 5. The locked model

### 5.1 Composites (all 56 features consumed; asserted 56/56)
**Capability** composites are oriented (higher = better) and feed LEVEL and the
offense/defense clashes; **descriptor** composites are style-neutral and enter as A−B
differences.

```
# capability (oriented) — 13 composites
STR_PRESS  = slpm15 + tot_lpm15 + sig_acc + str_diff15
POWER      = kd_per100 + kd15 + ko_win_share
TARGET_ACC = head_acc + body_acc + leg_acc
WR_OFF     = td15 + tda15 + td_acc + ctrl_share + ctrl_diff + td_diff15
GRD_THREAT = sub15 + sub_win_share + sub_per_ctrlmin + ctrl_per_td + grapple_index
             − str_per_tda + ground_lpm15
STR_DEF    = sig_def − sapm15 − tot_apm15 − o_head_share
WR_DEF     = td_def − o_ctrl_share − td_conceded15
SCRAMBLE   = rev15 − sub_faced15                 # getups / not caught
CHIN       = −kod_rate − kd_abs15
SUB_DEF    = −subd_rate
DURABLE    = −finished_against
CARDIO     = late_output_ratio − late_abs_ratio
FINISHER   = finish_rate − dec_share − avg_fight_min

# descriptors (neutral, differenced in the matchup)
RANGE/CLINCH/GRDSTR = dist_share / clinch_share / ground_share    # where they strike
LEG/BODY/HEAD       = leg_share / body_share / head_share         # what they target
DEF_DIST/DEF_GRD    = o_dist_share / o_ground_share               # where they get hit
SOUTHPAW/SWITCH     = is_southpaw / is_switch                     # stance
PHYS                = reach_in + height_in + reach_adv
AGE                 = age
RESULTS             = win_rate      # QUALITY / skill-prior slot — excluded from style edge
```
All features z-scored within the men's pool. `LEVEL = mean(13 capability composites)`;
`shape x_s = x − LEVEL`.

### 5.2 The matchup vector (asymmetric, order-invariant)
For a bout A vs B, `x(A,B)` is antisymmetric (`x(B,A) = −x(A,B)`), so there is no
first/second-fighter bias. Offense/defense clashes use shapes; descriptors are
differences:

```
x = [ LEVEL_A − LEVEL_B,                              # (quality — see 5.4)
      RESULTS_A − RESULTS_B,                          # (quality — see 5.4)
      od(STR_PRESS, STR_DEF),   od(TARGET_ACC, STR_DEF),
      od(POWER, CHIN),          od(WR_OFF, WR_DEF),
      od(GRD_THREAT, SCRAMBLE), od(GRD_THREAT, SUB_DEF),
      finish_resistance(DURABLE vs opp POWER),
      diff(CARDIO_s), diff(FINISHER_s),
      diff(RANGE), diff(CLINCH), diff(GRDSTR),
      diff(LEG), diff(BODY), diff(HEAD),
      diff(DEF_DIST), diff(DEF_GRD),
      diff(SOUTHPAW), diff(SWITCH),
      diff(PHYS), diff(AGE) ]
   where od(o,d) = (o_A^s − d_B^s) − (o_B^s − d_A^s)
```

### 5.3 Calibration and the two objects
`P = σ(β·x)` is a logistic on the 4,418 bouts. Two objects come out of it:

- **Full model** (23 terms incl. LEVEL + `win_rate`): AUC **0.775**, but ~all lift is the
  `win_rate` skill prior (§4.5). Used only as evidence that a real Elo term belongs
  there (§6).
- **The style edge (the locked deliverable):**
  ```
  s(A,B) = β_style · x_style(A,B)      # the 21 behavioural clash terms; LEVEL & win_rate dropped
  P_style = σ(s)                       # 50% = a stylistic coin flip
  ```
  Pure-style CV **AUC = 0.661** — level-neutral style alone beats a coin flip and roughly
  matches level alone. This is the number that composes with Elo.

---

### 5.5 Non-MMA pedigree (v1.1 extension — kept, but confirmation-only)
Non-MMA bouts (Tapology: 1,329 grappling, 775 kickboxing, 343 boxing, … across 645 of our
fighters) carry **no granular stats — only results** — so they cannot be behavioural
*features*. Instead each fighter gets three **pedigree** composites from their non-MMA
record, `log1p(n) · laplace_winrate` per discipline group (0 when absent):
```
STRIKE_PED  ← boxing / kickboxing / muay / karate / knuckle / lethwei / sanda / shootboxing
GRAPPLE_PED ← grappling / combat_jj
WRESTLE_PED ← wrestling / sambo / judo
```
They enter as three antisymmetric difference clashes (`add_nonmma_pedigree` in
`style_matchup.py`). Scores are face-valid (Adesanya 3.40 / Pereira 2.72 strike; Burns
2.09 grapple; Khabib 0). Weights are correctly signed (+0.06 grapple, +0.04 wrestle,
+0.01 strike).

**Finding — as a feature, confirmation-only.** The aggregate lift is **negligible —
style-edge AUC 0.6609 → 0.6629** — and does not grow for thinner UFC samples. Pedigree is
**redundant with the MMA stats** (a kickboxer already strikes like one in the cage; the
base *confirms* the MMA-derived style) and **sparse** (most matchups are 0-vs-0). So it is
kept but does not move established-fighter predictions.

**Finding — as a SEED, it works (`seed_test.py`).** The real use is a **day-one prior**
for a fighter's earliest UFC bouts, where the MMA vector doesn't yet exist. Seeding a
debutant from pedigree + physicals + stance (all MMA composites zeroed) predicts their
first-3-fights (n=1,146) at **logloss 0.679**, beating both a physicals-only seed (0.682)
and the constant base rate (0.688) — i.e. a debutant with no cage footage still gets a
better-than-nothing style prediction, and the pedigree adds a small correctly-signed
nudge. It is **face-valid**: at their UFC debut the seed lifts Roger Gracie 0.53→0.67,
Henry Cejudo 0.66→0.77, Rodolfo Vieira/Maia/Sperry/Shields ~+0.07 each — the grapplers and
Olympic wrestlers correctly elevated before their first cage minute, most of whom won.
**Conclusion: non-MMA bouts belong in the seeding layer, not the feature layer.**

## 6. Integration with Elo (locked design)

The style edge is a **matchup layer, not a standalone predictor** — analogous to promotion
strength being a component consumed by Elo:
```
logit P(A beats B) = a · (Elo_A − Elo_B) + s(A,B)
```
Elo supplies "who is better"; `s(A,B)` supplies "but this style troubles that one." The
`win_rate`/`LEVEL` terms in §5 are the *interim* stand-in for `Elo_A − Elo_B`; wiring the
real Elo from `system_a_elo_ps` into that slot is the one remaining step.

---

## 7. Validation

**Nested models (5-fold CV, 4,418 bouts):**

| model | AUC | log-loss | note |
|---|---|---|---|
| full 23-term (incl. win_rate) | 0.775 | 0.569 | dominated by the `win_rate` skill prior |
| `win_rate` diff **alone** | 0.765 | 0.577 | ≈ the label — quarantined to the prior slot |
| all 55 behavioural (no win_rate) | 0.694 | 0.633 | honest "every behavioural feature" |
| **style edge** (no LEVEL, no win_rate) | **0.661** | 0.656 | **the locked object** |
| *(original 7-term style model)* | 0.652 | — | for reference — the richer set added ~1 pt |
| coin flip | 0.500 | 0.693 | baseline |

**Style edge on the fights we studied** (`P_style` = A's chance on style alone):

| matchup | P_style (A) | top drivers |
|---|---|---|
| Islam vs Garry | 45% | power/chin (B), sub-threat (A), striking-D (A) |
| Islam vs Prates | 49% | power/chin (B), sub-threat (A), striking-D (A) |
| Islam vs Morales | 40% | power/chin (B), size (B) |
| Islam vs Shavkat | 42% | physical (B), power/chin (B) |
| Islam vs Strickland | 36% | sub-threat (A) vs volume/chin/cardio (B) |
| Strickland vs Chimaev | 19% | Chimaev's grappling shape dominates on paper |

**Market calibration (external check).** Vig-removed lines vs the model:

| fight | market (fair) | style + result |
|---|---|---|
| Chimaev vs Strickland | ~79% Chimaev (−500) | style favours Chimaev (~81%); **Strickland won (upset)** |
| Islam vs Garry | ~74% Islam (−370) | style ~even (Islam 45%); **Islam won on level** |

Reading, corrected for the actual results:
- **Chimaev–Strickland is not a model miss.** The style edge favoured Chimaev, *as did
  the −500 market*. A calibrated probability model is not falsified by the underdog
  winning — Strickland's performance was a genuine, unprecedented low-probability event.
  The model correctly identified the heavy favourite; the upset is the tail. (What *is* a
  fair caveat: on thin résumés like Chimaev's 9 fights, the **magnitude** of the edge is
  less certain than for a 30-fight veteran — a confidence-interval point, not a
  wrong-favourite point.)
- **Islam–Garry validates the design.** Style is ~even (45%); Islam won because he is the
  better fighter (LEVEL/Elo), *not* because his style dominates Garry's — which matches
  the competitive fight (Garry out-struck him; Islam won on control). Add Elo and Islam is
  correctly favoured, exactly as the market had it.

The recurring lesson: the style edge tells you **where the fight is close or where the
upset lives**, and Elo tells you who should win. Neither replaces the other.

---

## 8. Limitations and future work

1. **Quality lives in Elo, not here — by design.** The style edge intentionally says
   nothing about who is the better fighter. Used alone it will call even fights a coin
   flip (correctly); it must be summed with Elo (§6) for a full prediction.
2. **Thin résumés give noisy-magnitude vectors.** Chimaev (9 fights), Shavkat (7) have
   less certain shapes than 20–30-fight veterans. The *direction* is usually right; the
   *size* is wide. Optional fix: raise the vector threshold to ≥ 10 fights.
3. **Career aggregates miss fight-specific dynamics.** Getups, gas-tank collapse,
   round-to-round adjustments, size-at-a-new-weight. The hand read of Chimaev–Strickland
   beat the aggregate model by watching the *tape* (Strickland's getups). Aggregates
   can't see that.
4. **Out-of-division moves are out of distribution** ("Islam at 185"); `PHYS` is a weak
   proxy. Do not trust across weight jumps.
5. **Missing granular inputs.** No top/bottom control split, cage position, or strike
   type — see the data-gap analysis. Top/bottom would sharpen `SCRAMBLE`/`WR_DEF` most
   (a top smother vs a guard player are one vector today). Cheapest win: scrape
   statleaders top/bottom.
6. **Collinear grappling terms.** The wrestling/ground/sub clashes are correlated, so an
   individual term's weight understates its axis (Islam's wrestling `x`≈+15 gets β≈+0.02).
   **Read term *clusters*, not single terms.**
7. **Women's model** fit separately on a smaller pool; weights not reported here.
8. **Non-MMA pedigree: feature vs seed vs blend (§5.5).** Confirmation-only as a
   *feature*; works as a debut *seed*; the continuous *blend* (`apply_pedigree_blend`,
   `w = n/(n+6)`) correctly shrinks small-sample inflation (Chimaev's shape 59→39) but is
   **opt-in, not default** — it cannot be validated by the current *retrospective* harness
   (career vectors leak the target fight, so shrinkage looks worse: thin-subset logloss
   0.611→0.615). Both the seed and the blend are *prospective* properties; validating the
   blend needs **as-of-fight-date vectors** (each built only from prior fights), a proper
   prospective backtest the pipeline does not yet produce. That backtest is the real next
   step, not more shrinkage tuning.

---

## 9. Parameter reference

| Parameter | Value | Role |
|---|---|---|
| Feature coverage | 56 / 56 | every numeric profile feature used |
| Capability composites | 13 | oriented; feed LEVEL & off/def clashes |
| Descriptor composites | 10 | style-neutral differences |
| Matchup vector | 23 terms | LEVEL + win_rate(quality) + 21 style |
| **Style edge** | **21 behavioural terms** | LEVEL & win_rate excluded — the locked object |
| Min career fights | 8 (fit) / 5 (vector) | stability |
| Scope | all-time UFC, complete-stats | 10,765 vectors; 4,418 fits |
| Calibration | logistic, `C=1.0`, 5-fold CV | outcome fit |
| Style-edge AUC | 0.661 | level-neutral benchmark |
| Intended level input | Elo (§6), not `win_rate` | quality term |

---

## Appendix: worked style edge — Islam Makhachev (A) vs Carlos Prates (B)

**Composites (shape = level-removed):**
```
Islam  : WR_OFF +9.67  GRD_THREAT +5.66  STR_DEF +5.59  CHIN −1.89  DURABLE −1.62  POWER −0.99
Prates : POWER +9.44   FINISHER +3.30    CHIN +2.10     DURABLE +1.69  WR_OFF −6.09  STR_DEF −1.50
(LEVEL: Islam +2.09 — excluded from the style edge)
```

**Top clash contributions `β·x` (+ favours Islam):**
```
power_vs_chin   −1.74   Prates's nuclear power vs Islam's proven-breakable chin
finish_resist   +1.07   (pairs with the above; net power/durability ≈ −0.66 Prates)
sub_vs_subdef   +0.91   Islam's submission threat vs Prates's modest sub defense
striking        +0.63   Islam's elite defense wins the striking exchange
ground/scramble −0.42 , def_dist −0.41 , physical −0.39 , range −0.37 , wrestling +0.33 , …
```

**Aggregate:**
```
s = Σ β·x = −0.040        (log-odds, level-neutral)
P_style   = σ(−0.040) = 0.49   → 49% Islam on pure style
```
Reading: the grappling cluster (wrestling + sub-threat + ground ≈ **+0.8 Islam**) and
Islam's elite striking defense (**+0.63**) are cancelled by the power/durability cluster
(**≈ −0.66 Prates**) plus size and range. On pure style it is a **coin flip** — Prates's
one-punch KO power against Islam's one breakable trait is a genuine equalizer. Add Elo
(Islam ≫ Prates) and Islam is the clear favourite, with Prates's live path being exactly
that `power_vs_chin` term.
