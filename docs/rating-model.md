# The rating model

> Related: `promotion-strength-v2.md` (what a card is worth) · `cross-sport-transfer.md`
> (what a rating in one sport is worth in another) · `fighter_style.md` (how two specific
> fighters match up)

  R(f, s, t)   how good fighter f is at sport s, right now, on that sport's own scale

This is the definition the other model docs attach to. It fixes what the number means, what
state is carried, how a bout changes it, and which term each input doc supplies. It defines
no data and ships no script; everything here is the contract the engine implements.

## 1. What the number quantifies

Elo has no meaning apart from its likelihood -- the rating is defined by what it is asked to
predict. Three decisions fix it.

**Per sport, not per athlete.** The reported number is sport-specific. A general combat
ability exists in the model, but it is latent and never shown, because the data says it is
real and incomplete: transfer between sports is positive in all 19 measured ordered pairs and
complete in none (`cross-sport-transfer.md`). One number per fighter would assert a single
latent trait the evidence does not support; independent per-sport numbers would throw away
transfer that is worth a 2.4% cut in Brier score.

**Present ability, not career achievement.** `R` is a running estimate of how good a fighter
is on a given date. This is why age and inactivity belong inside the model rather than beside
it, and why a career or peak number -- a "greatest of all time" statistic -- is a different
object built from the trajectory `R(f, s, ·)`, not read off it.

**Anchored per sport.** Each sport carries its own 1500. A boxer's 1600 and an MMA fighter's
1600 are **not** the same quantity, and are related only through `kappa`. Stated here so no
consumer has to infer it.

## 2. State

For each fighter, a vector over the sports they have contested:

```
  R(f, s, t)  =  1500 + G(f, t) + delta(f, s, t)          sum_s delta ~ 0
```

`G` is general combat ability, `delta` is ruleset specialisation. The engine does not have to
represent `G` and `delta` separately: propagating updates between components with `kappa`
(section 4) is equivalent, and is what the measured asymmetry requires. `G + delta` is the
rank-1 reading of the same object, useful for talking about it.

A fighter who has only ever contested one sport has one component. Nothing else is stored,
nothing else is updated, and the model reduces exactly to ordinary Elo for them -- which is
about 92% of the roster.

## 3. Prediction

```
  logit P(A beats B in sport s at t)  =  a * [ R(A,s,t) - R(B,s,t) ]  +  s(A,B)
```

`a = ln(10)/400` keeps the usual Elo scale. `s(A,B)` is the style edge from
`fighter_style.md`: asymmetric, order-invariant, and deliberately **not** part of the rating.
The division of labour is the point:

```
  G(f,t)       who is the better athlete
  delta(f,s,t) how well that transfers to this ruleset
  s(A,B)       how these two specifically match up
```

Anything that is a property of the pairing belongs in `s`, never in `R`. Anything that is a
property of the fighter belongs in `R`, never in `s`.

## 4. Update

After a bout in sport `s`:

```
  Delta      =  K * w * ( outcome - expected )

  R(f, s)   +=  Delta
  R(f, s')  +=  kappa(s -> s') * Delta            for every other component
```

`kappa` comes from `cross-sport-transfer.md` -- one constant per ordered sport pair, tested
against three conditionings and found to need none. The propagation is what makes the vector
a vector rather than a set of unrelated ratings.

`w` is the weight of the evidence, and is where the input models enter:

```
  w  =  w_promotion  *  w_outcome  *  w_recency  *  w_stakes
```

| factor | supplied by | status |
|---|---|---|
| `w_promotion` | `promotion-strength-v2.md`, `norm` in [0,1] with 0.5 neutral | **defined** |
| `w_outcome` | how decisively the result was reached -- finish vs split decision, and round-level performance where it exists | open |
| `w_recency` | inactivity and layoff | open |
| `w_stakes` | title stakes from `bout-title-stakes.md` | open, may be 1 |

Age enters as a drift on `R` between bouts rather than as a factor on `w`: a fighter who does
not compete for two years is not less informative, they are different. That is a separate
term and it is not yet defined.

## 5. Initialisation

A fighter's first bout in a sport needs a prior. Two sources, in order of precedence:

```
  cross-sport, when they have >= 3 professional bouts in another sport:

     R_seed(f, b)  =  1500 + kappa(a -> b) * c_a * ( q_a - qbar_a )

  otherwise, the card they debut on:

     R_seed(f, b)  =  1500 + 150 * ( promotion_strength - 0.5 )
```

`c_a` and `qbar_a` are in `data/cross_sport_transfer.json`; `q` is the Laplace-smoothed record
quality used throughout the project. The precedence is a decision, not a measurement: the
cross-sport seed is worth an 8% cut in seeding error where it applies, the promotion seed is
all that exists otherwise, and no attempt is made to combine them. Combining them properly
needs a prior variance for each, which nobody has estimated.

## 6. Invariants

**Circularity.** Nothing that feeds `R` may be derived from `R`, or from any other rating
system's output. This is why promotion strength is built from records and cards, why `kappa`
is estimated from outcomes, and why Fight Matrix's promotion orderings are a validation
target and never an input.

**Causality.** Every term is computed from information strictly before the bout it weights.
Promotion strength uses a window that excludes the event's own date; `kappa`'s seed uses the
record carried in; a reign counts toward `elite` only if won before `t`.

**Zero sum within a pool.** Rating points move between the two fighters in a bout of sport
`s`. Cross-sport propagation is the one exception and is deliberately not zero-sum: a boxing
win adds `kappa * Delta` to the winner's MMA component without taking it from anyone. The
component drifts against its own sport's population as a result, which is acceptable while
`kappa < 1` and crossovers are ~8% of fighters, and would not be at scale.

## 7. Worked example

Gökhan Saki, 28-9 in kickboxing, `q = 0.755`, debuting in MMA on 2017-09-22.

```
  c_KB = 682.5    qbar_KB = 0.417    kappa(KB -> MMA) = 0.28    kappa(MMA -> KB) = 0.309

  R_kickboxing  = 1500 + 682.5 * (0.755 - 0.417)             = 1731
  R_mma  (seed) = 1500 + 0.28 * 682.5 * (0.755 - 0.417)      = 1565
```

He then wins an MMA bout he was a slight underdog in (expected 0.45), at K = 32 and w = 1:

```
  Delta = 32 * (1 - 0.45) = +17.6

  R_mma          1565 -> 1582        full credit in the sport contested
  R_kickboxing   1731 -> 1736        +0.309 * 17.6, the part that generalises
```

The present engine cannot express this. It carries one rating, damps K by a hand-set
`translation_to_mma`, and would move his single number by roughly +9 -- so the fact that he
got better at MMA and barely changed as a kickboxer has nowhere to live.

## 8. What is defined and what is open

| piece | doc | state |
|---|---|---|
| estimand, state, prediction, update | this doc | defined |
| `w_promotion` | `promotion-strength-v2.md` | defined, validated against two external sources |
| `kappa`, cross-sport seeding | `cross-sport-transfer.md` | defined, validated out of sample |
| `s(A,B)` | `fighter_style.md` | defined, UFC men's only, not fitted for other pools |
| age drift | -- | **open, next** |
| inactivity / layoff | -- | open |
| `w_outcome` | -- | open |
| `K` itself, and evidence tiers | -- | open |

## 9. Open decisions

1. **`K` is undefined.** Everything above treats it as given. Its size, whether it decays with
   a fighter's bout count, and how evidence tiers change it are not settled here.
2. **Seed precedence is asserted.** Cross-sport beats promotion strength when both apply, with
   no variance-weighted combination.
3. **Cross-sport propagation is not zero-sum** (section 6), and the drift it induces has not
   been measured.
4. **Bare-knuckle boxing and boxing may be one sport.** `kappa` between them is 0.97, which is
   near-complete transfer. Treating them as separate components with a coefficient close to 1
   is a modelling choice that should be made deliberately rather than absorbed.
5. **`s(A,B)` is fitted for UFC men only**, so the prediction equation is incomplete for every
   other pool the rating covers.
