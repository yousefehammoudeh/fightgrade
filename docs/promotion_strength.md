# Promotion Strength: An Elo-Free, Time-Varying Measure of MMA Promotion Quality

**Status:** Superseded by `promotion-strength-v2.md` -- kept because the reasoning below
(especially the rejected approaches in section 4) is why v2 is shaped as it is.
**Was:** Locked (v1)
**Scope:** MMA only
**Consumed by:** `system_a_elo_ps` — entry-Elo seeding and per-fight K weighting

---

## Abstract

The Elo replay engine weights each fight by the strength of the promotion it
occurred in: a title fight in a top organization should move ratings more, and
seed debutants higher, than a bout on a small regional card. This creates a
bootstrapping problem — promotion strength cannot itself be derived from fighter
Elo, because Elo depends on it. We therefore need an **Elo-independent** estimate
of how strong a promotion was **at a specific point in time**.

This document describes the measure we arrived at: a **time-varying, field-relative
composite** built from four causal signals — roster intake quality, scheduling
consistency, matchmaking competitiveness, and roster scale — computed over a
trailing window and squashed into a `[0, 1]` multiplier. We record not only the
final formula but the full path to it, including two approaches (a hand-set
baseline and a talent-flow centrality) that we tried and rejected, because their
failure modes justify the final design.

---

## 1. Motivation

### 1.1 The circularity constraint

The ranking pipeline weights a bout's K-factor and seeds a debutant's entry
rating by `promotion_strength ∈ [0, 1]` (see `system_a_elo_ps.py`:
`compute_entry_elo`, `_promotion_multiplier`). Because that value feeds Elo, it
**must not be computed from Elo output** — otherwise strong promotions and strong
fighters would define each other in a loop. Every signal in this measure is
derived only from *fight records and event metadata*, never from a rating the
pipeline produces.

### 1.2 Strength is not a constant

A single all-time number cannot represent a promotion whose quality changed over
its lifetime. PRIDE FC was arguably the UFC's equal in 2005 and defunct by 2007;
the UFC itself was one of two co-leaders in the mid-2000s and a decisive monopoly
a decade later. The measure must be a **function of time** — `strength(promotion, t)`
— so the replay can ask "how strong was this promotion on the night of this
fight?"

---

## 2. Data

All signals are computed from the cached Tapology dataset (`seeder_data/data/`):

- `tapology_bouts.csv` — one row per bout: `sport`, `is_title_fight`,
  `weight_class`, `method`, `event_date`, `promotion_tapology_id`, event slug.
- `tapology_bout_fighters.csv` — one row per fighter-bout: `result`,
  `fighter_record_before_raw` (the fighter's W-L-D *entering* the bout),
  opponent, source.
- `tapology_events.csv`, `promotions_with_sports.csv` — event and promotion
  reference data.

**Scope: MMA only.** We filter to `sport == 'mma'` (66,704 bouts). This is a
deliberate, load-bearing limitation (see §8): promotions such as ONE Championship
and RIZIN run large Muay Thai / kickboxing calendars that are almost entirely
absent from this MMA-seeded dataset, so their *MMA* footprint understates their
total operation.

Incoming records parse cleanly: 91% are `W-L` or `W-L-D`, ~9% empty, no malformed
rows. Amateur bouts are effectively absent (0.5%), so professional/amateur status
carries no signal here.

---

## 3. Design goals

A good promotion-strength measure should:

1. **Be Elo-independent** (§1.1).
2. **Vary with time**, so it reflects a promotion's quality at each date (§1.2).
3. **Rank the right promotions on top.** We validate against two external,
   current ground-truth tierings: Tapology's **"Major Org"** event classification
   and **Fight Matrix's** promotion ranking.
4. **Be geography-neutral.** Strong non-US promotions (ONE, ACA, RIZIN, KSW)
   must not be penalized for operating outside the UFC's talent pipeline.
5. **Reproduce the UFC monopoly and its arc** — decisive dominance after the 2007
   PRIDE acquisition, peaking in the mid-2010s, softening as PFL/Bellator/RIZIN
   matured.
6. **Emit a bounded `[0, 1]` multiplier** matching the Elo code's contract, where
   `0.5` is the neutral (average) promotion.

Goals 4 and 5 are in tension (see §4.6): dominance requires a *scale* term, but
scale penalizes small-but-major promotions. The final model resolves this with a
single tunable exponent.

---

## 4. Methodology: how we got here

### 4.1 Baseline: the hand-set column (rejected)

The original `promotions_with_sports.csv` carried a hand-authored
`baseline_strength ∈ [0, 1]` (UFC = 1.00, PRIDE = 0.90, Bellator = 0.86, …). It
is a reasonable expert prior for the ~25 marquee promotions but arbitrary for the
2,000-promotion long tail (248 promotions share a handful of round "tier" values;
the rest are jittered). It is also static and UFC-anchored. We keep it only as a
**supervised reference for the marquee promotions**, not as the measure.

### 4.2 Signal 1 — Intake (roster quality)

The founding intuition: **strong promotions sign fighters who already have strong
records.** For each fighter's *first* bout in a promotion (their debut there), we
read the record they carried in and score it:

```
winrate    = (W + 0.5·D + 1) / (W + L + D + 2)      # Laplace-smoothed
experience = N / (N + K_exp),   N = W + L + D,  K_exp = 10
intake_i   = α · winrate + (1 − α) · experience,    α = 0.6
```

Laplace smoothing gives a true 0-0 debutant a neutral 0.5 rather than an
undefined ratio; the experience term separates proven veterans from padded
prospects. A promotion's intake is the mean over all its debutants in the window.

**What this alone gets wrong:** it measures the *selectivity of a destination*.
It ranks prospect-feeder shows (DWCS) above the UFC (their signings arrive with
more padded, undefeated records), and it inverts entirely for **entry-point**
promotions — Shooto and Pancrase, where elite Japanese fighters make their *pro
debut* at 0-0, land at the very bottom despite being historically strong.

### 4.3 The talent-flow detour (rejected)

To rescue entry-point promotions we tried measuring the *other side of the
flow* — where fighters go **after** a promotion — via graph centrality on the
fighter-movement graph:

- **Directed PageRank** on net career transitions (A→B when fighters climb from A
  to B). This over-concentrated on the UFC and its direct feeders and *un*-rescued
  Shooto/Pancrase (they feed the PRIDE-era Japanese scene, not the modern UFC).
- **Undirected eigenvector centrality** on the shared-roster graph (promotions
  linked when fighters appear in both). Better — it rescued the historical greats
  — and it became our "exchange" signal for a while.

**Why we ultimately dropped exchange entirely.** When we finally tuned against
Tapology's Major Org label, exchange-centrality was revealed as *actively
harmful*. It measures **integration with the global talent web**, which the UFC
ecosystem dominates. It therefore punishes exactly the strong-but-insular non-US
majors — ONE (norm rank #16 → its exchange ratio *fell* from 0.43 to 0.11 as it
became more self-contained), ACA, RIZIN, KSW. Tuning to maximize major/regional
separation drove exchange's weight to **zero**. It is not in the final model.

This is the central lesson of the project: **connectivity is not quality.** A
promotion can be strong within an isolated market (peak PRIDE in Japan, ONE in
Asia) and any graph-integration measure will underrate it.

### 4.4 Fight Matrix as a reference point

Fight Matrix ranks promotions by the **summed rating (point total) of their
ranked fighters** — a pure bottom-up aggregate of individual fighter ratings
(their proprietary Elo-like CIRRS). We cannot adopt this (it is Elo-derived, and
therefore circular for us), but it confirmed two things: (a) roster **depth ×
quality** is what makes a promotion look dominant, and (b) our target ordering
should place the UFC clearly first with a long tail below — which motivated the
scale term (§4.6).

### 4.5 De-anchoring and going time-varying

Two structural changes fixed goals 2 and 4:

- **Field-relative normalization.** Instead of dividing by the UFC (which caps the
  UFC as the ceiling in every era — historically false, and it hides the UFC's own
  growth), each signal is expressed **relative to the mean of the active field**
  that period. `1.0` = the era's average promotion; a promotion can exceed any
  other, including the UFC.
- **Trailing window.** Every signal is computed over bouts in the window
  `(t − 3 years, t]`. Computing the same formula causally at each date turns it
  into a time series and **dissolves the era problem**: PRIDE computed *as of 2005*
  is a strong #2; PRIDE computed over all-history-to-now is noise.

With these, the historical arcs become correct: PRIDE at parity with the UFC in
2005, Strikeforce peaking in 2010–11, Bellator peaking ~2011 and declining, the
WSOF→PFL rebrand handing off around 2017.

### 4.6 Scale, and the monopoly/inclusion tension

Field-relative *averages* (intake, consistency, matchmaking) are bounded, so they
flatten every major to ~1.0–1.2 and the UFC cannot show a monopoly. Restoring it
requires a **scale** signal — roster depth — which is non-circular and, in raw
terms, enormous (UFC ≈ 875 active MMA fighters vs. ~100 for its nearest
independent rival, ~3.9× at the 2017 peak).

But scale reintroduces the insular-major problem: ONE has only ~19 MMA fighters,
so raw scale buries it. The resolution is to keep the two jobs separate and give
scale a **compressing exponent**:

```
strength = quality × scale^k
```

`quality` (the field-relative composite) decides *who is a major* (geography-
neutral); `scale^k` decides *the UFC's dominance margin*. The single exponent `k`
trades the two off: `k = 0` is a pure tier (no monopoly, ONE safe); large `k` is a
crushing monopoly (ONE buried). We lock `k = 0.30`, which yields a decisive-but-
not-absurd UFC lead (≈ 1.6× the #2 today, ≈ 1.7× at peak, parity with PRIDE in
2005) while keeping every major in the top tier.

### 4.7 Shrinkage

Intake and matchmaking are means over small samples for minor promotions, which
produced spurious top-20 finishes for 5–7-fighter shows. We apply empirical-Bayes
shrinkage toward the global mean before field-normalizing:

```
shrunk_i = (n_i · raw_i + m · global_mean) / (n_i + m)
```

with pseudo-counts `m_intake = 25`, `m_match = 50`. Consistency and scale are
already volume-sensitive (log/roster terms) and are not shrunk.

---

## 5. The locked model

For a promotion `p` at date `t`, over the window `W = (t − 1095 days, t]`:

**Per-signal raw values (MMA bouts in `W`):**

| Signal | Definition |
|---|---|
| `intake` | shrunk mean of `α·winrate + (1−α)·experience` over debutants in `W` |
| `consistency` | `(active_years / span_years) · log(1 + event_count)` |
| `matchmaking` | shrunk mean per-bout record mismatch `|q_A − q_B|` (lower = better) |
| `roster` | count of distinct fighters with a bout in `W` |

**Field.** The *field* is the set of promotions with `≥ MINF = 8` MMA fighters in
`W`. Field membership defines the normalizing means/median only; **all**
promotions are scored (the gate is not applied to scoring).

**Field-relative ratios** (÷ field mean; matchmaking inverted so higher = better):

```
I = intake_p      / mean_field(intake)
C = consistency_p / mean_field(consistency)
M = mean_field(mismatch) / mismatch_p
S = roster_p      / median_field(roster)
```

**Composite:**

```
quality  = 0.60·I + 0.25·C + 0.15·M
strength = quality × S^k,              k = 0.30
```

**Normalization to the Elo multiplier:**

```
norm = 1 / (1 + exp(−A · ln(strength / M_norm))),   M_norm = 1.0,  A = 1.8
```

A logistic on `ln(strength)` is symmetric in ratio space and maps the field
average (`strength = 1.0`) to exactly `0.5` — the Elo code's neutral point. The
UFC lands ≈ 0.93 today; a hypothetical stronger promotion can approach 1.0.

**Fallback (locked).**

- Promotions below the field threshold are still scored (they receive genuinely
  low ratios), **not** dropped.
- A promotion with **no** MMA bouts in `W` (brand-new, or a gap year) has no
  computable strength and is assigned `norm = 0.30` — a below-average default.
  This overrides the Elo code's previous `None → 1.0`, which would have wrongly
  granted unknown promotions full weight.

**Causality (locked).** A card's weight is computed **per event, strictly before**
its own date: the window is `(t − 1095 days, t)` with `t` exclusive, so a fight
never contributes to the promotion strength that weights it. For large promotions
this is negligible; for tiny ones it prevents self-reference.

**Cross-era stance (locked).** The logistic is **fixed** across all eras (no
per-era re-anchoring). Consequently a top-promotion fight weights *less* in a
weaker era — UFC-2005 ≈ 0.79 vs UFC-2025 ≈ 0.93. This is intentional: fights of
that era were against a genuinely shallower global talent pool, and the K-factor
should reflect it.

---

## 6. Integration with Elo

`system_a_elo_ps` consumes `norm` in two places (both treat `0.5` as neutral):

- **Entry seed:** `rating = 1500 + 150 · (norm − 0.5)` → debut in the UFC seeds
  ≈ +64, in an average promotion +0, in a tiny show toward −60.
- **K multiplier:** `k *= clip(norm, 0.10, 1.0)` → a UFC result counts ≈ 0.93×
  base K, an average promotion 0.5×, a tiny show floored at 0.10×.

---

## 7. Validation

**Major-Org separation.** Tuned against Tapology's 9 Major-Org MMA promotions
(Invicta excluded as women-only, not comparable on these signals), the quality
signals separate majors from regionals with **AUC ≈ 0.99**. All majors land in the
top tier; ONE is last among them (#21 overall), consistent with both Tapology and
Fight Matrix placing ONE last and with its small MMA footprint.

**Fight Matrix agreement.** UFC #1 matches. We rank large-roster US promotions
(DWCS, LFA) above the smaller non-US majors — a known consequence of the scale
term — whereas Fight Matrix, ranking by pure fighter talent, orders them below.

**Historical arc (normalized `[0,1]`).**

```
year   UFC   PRIDE  Strikeforce  Bellator  PFL   ONE
2005   0.79   0.79       ·          ·       ·     ·     parity with PRIDE
2011   0.92    ·        0.88       0.84     ·     ·
2015   0.95    ·        0.66       0.88     ·    0.68   UFC peak
2025   0.93    ·         ·         0.72    0.78  0.58
```

The UFC/#2 ratio moves 0.98× (2005, behind PRIDE) → 1.23× (2007 buyout) → 1.72×
(2019 peak) → 1.58× (2025) — the documented monopoly and its softening.

---

## 8. Limitations and future work

1. **MMA-only.** ONE and RIZIN run large striking calendars absent from this
   dataset (ONE: 36 MMA vs. ~11 non-MMA bouts recorded for 2023–25, versus
   hundreds actually held). Their scale and consistency are therefore
   *understated*. The principled fix is a **per-sport, per-time** strength that
   would require scraping the striking calendars; until then, MMA-only is the
   honest scope.
2. **Scale favors integrated markets.** The roster term rewards large US-pipeline
   promotions (DWCS, LFA) over compact elite ones. This is a deliberate trade
   controlled by `k`.
3. **Concentrated elite cores are invisible to averages.** A promotion whose
   greatness is three transcendent fighters (peak PRIDE: Fedor, Wanderlei,
   Nogueira) is not fully captured by mean-based signals. A top-end talent-density
   signal is possible future work.
4. **Field drift.** "Relative to the average promotion" shifts as the population
   of tracked promotions grows over time; cross-era comparisons are era-relative
   by construction (see §5, cross-era stance — accepted).

---

## 9. Parameter reference

| Parameter | Symbol | Value | Role |
|---|---|---|---|
| Trailing window | `WIN_DAYS` | 1095 (3 yr) | data horizon per query |
| Experience saturation | `K_exp` | 10 | fights for 0.5 experience |
| Intake win-rate weight | `α` | 0.60 | win-rate vs. experience |
| Intake shrinkage | `m_intake` | 25 | pseudo-count |
| Matchmaking shrinkage | `m_match` | 50 | pseudo-count |
| Field threshold | `MINF` | 8 | fighters to enter field mean |
| Quality weights | — | 0.60 / 0.25 / 0.15 | intake / consistency / matchmaking |
| Scale exponent | `k` | 0.30 | monopoly dial |
| Norm midpoint | `M_norm` | 1.0 | strength mapped to 0.5 |
| Norm steepness | `A` | 1.8 | logistic slope |
| Missing-data default | — | 0.30 | no bouts in window |
| K-multiplier floor / cap | — | 0.10 / 1.0 | `system_a_elo_ps` |

---

## Appendix: worked example (UFC, end of 2025)

```
roster (3-yr) = 875 fighters
I = 1.06,  C = 1.77,  M = 0.99          (field-relative ratios)
quality  = 0.60·1.06 + 0.25·1.77 + 0.15·0.99 = 1.23
S        = 875 / median_field_roster ≈ 875/15 → S^0.30 ≈ 3.35
strength = 1.23 × 3.35 ≈ 4.12
norm     = 1 / (1 + exp(−1.8 · ln(4.12))) ≈ 0.93
→ entry seed ≈ 1500 + 150·(0.93 − 0.5) = 1564
→ K multiplier ≈ 0.93
```
