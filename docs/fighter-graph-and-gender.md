# Fighter graph, population filter, and gender inference

Two decisions and one derived attribute, all computed from data already on disk. No
crawling is involved in any of this.

---

## 1. The fighter graph

Nodes are `fighter_slug` (with `fighter_id` where present -- slug and id are the only
identifiers; **name is never an identifier**, and duplicate names exist: two distinct
slugs are both "Craig Jones", one with 61 grappling bouts and one with 3).

Edges are contested bouts with two linked fighters.

```
nodes (distinct fighters)   467,263
edges                       820,163   (797,776 unique pairs)
ISLANDS                      42,360
LARGEST COMPONENT           368,891   (78.95%)
2nd largest                     102   <- a 3,600x gap
```

It is a textbook giant component: one connected world, then debris. **34,322 of the
42,360 islands are a single closed pair** -- two fighters who fought each other and
nobody else. The islands are not isolated scenes, they are one-and-done amateurs.

The giant component is essentially the professional world: only 3.7% of Professional
bouts fall outside it, against 12% of Amateur, 33% of Juniors and 48% of Kids.

---

## 2. DECISION (locked): the canonical modelling population is 365,907

Reproduced by `scrapers/derive/fighter_population.py` -> `data/fighter_gender.jsonl`
(one row per fighter in the corpus, `in_population` flags membership). ~20 s, no network.

```
population = giant component of the opponent graph
             MINUS youth-only fighters   (levels subset of {Kids, Juniors})
             MINUS closed pairs and isolated fighters
             MINUS fighters whose gender cannot be inferred
```

```
all fighters               467,263
  youth-only                   1,962   (51 kids-only, 1,902 juniors-only, 9 both)
  closed pairs                68,644
  isolated                        40
  outside the giant component 29,688 (islands of 3..102)
  gender-unlabelled in giant   2,187
CANONICAL POPULATION       365,907     (30,495 female / 335,412 male)
```

**Quote the population any figure is computed over.** Four are legitimate and differ:
467,263 (all) / 397,355 (filtered) / 368,099 (giant) / **365,907 (canonical)**.

### This is not a size optimisation

Measured against the 94,200 events that actually contain bouts, the giant-component
filter keeps:

| | kept | % |
|---|---|---|
| bouts | 764,243 / 821,046 | 93.08% |
| events | 93,806 / 94,200 | **99.58%** |
| promotions | 6,462 / 6,490 | **99.57%** |

Dropping 21% of *fighters* costs 7% of bouts and 0.4% of events. The excluded fighters
have one or two bouts each and almost never make up a whole card, so a twelve-bout event
with two debut-vs-debut bouts is kept, minus those two bouts.

A first pass appeared to cut 10% of events. That was an artefact: **10,197 events contain
zero bouts** (`zero_bouts_declared` -- cancelled cards, "no fight card available") and are
unattributable to any fighter by construction, so they drop under any fighter filter. If
size is the goal that predicate is the lever, not the graph -- though whether an
announced-then-cancelled card is signal for promotion strength is a separate question.

**The rationale is fighter quality**: no dangling pairs, no youth-only records, and 100%
gender coverage rather than 92.1%.

### Excluding the 2,187 gender-unlabelled costs almost nothing

| | with | without | lost | % |
|---|---|---|---|---|
| bouts | 764,243 | 763,379 | 864 | **0.113%** |
| events | 93,806 | 93,792 | 14 | 0.015% |
| promotions | 6,462 | 6,459 | 3 | 0.046% |
| title bouts | 59,036 | 59,015 | 21 | 0.036% |

1,409 of the 2,187 have a single bout and 461 have two -- 85% at two or fewer. The
alternative is **1,563 manual ground truths to retain 864 bouts**, roughly half a manual
lookup per bout recovered, on debut-level records at the edge of the graph. Excluding
them is also internally consistent: 68,644 fighters are already dropped for having thin,
weakly-connected records, and these are the same kind of fighter identified by a
different symptom.

## 3. Gender inference

There is **no gender field** anywhere in the data -- not on a bout, not on a fighter --
and only 562 title strings contain "Women". Shevchenko's 13 UFC title bouts read plain
`UFC Flyweight Championship`, identical to the men's. So gender is inferred.

### Method

Two-sided majority label propagation over the opponent graph.

1. **Seed** from title strings: `Women|Women's|Womens|Woman|Female|Feamale|Ladies` ->
   female; `Men's|Mens|Male` -> male. 1,737 female, 603 male.
2. **Propagate**: an unlabelled fighter takes the majority gender of their *labelled*
   opponents. Unlabelled opponents do not count in the denominator -- scoring against all
   opponents instead collapses recall to 79%, because early on almost nobody is labelled.
3. Iterate to convergence (20 rounds).

### Four fixes, each load-bearing

- **Majority, not reachability.** Plain BFS from female seeds labels 79% of all fighters
  as female, because a handful of intergender bouts weld the male and female populations
  into one component. Majority voting handles it with no sport exclusions: Craig Jones is
  1F/60M -> male, while Gabi Garcia survives the same edge at 34F/1M.
- **Exclude `modified_rules` edges.** These are 4x more likely to be intergender (1.9% vs
  0.533% baseline). Without the exclusion a man with few bouts gets labelled female off a
  single intergender bout and then *contaminates his own opponents*, who feed the label
  back -- a self-reinforcing pocket (Chistyakov -> Tarasov -> Perekhozhev).
- **Drop contradictory seeds** -- fighters seeded both ways.
- **Combined-event guard.** `2024 AFBC Elite Men's and Women's Boxing Championships` is a
  combined-sex event whose per-bout strings drop the "and Women's" half, labelling
  women's gold-medal matches `Men's`. **Refuse male seeding from any event whose name
  contains both markers.** This suppressed 98 bad seedings and took male->female errors
  from 3 to 0.

### Validation

5-fold cross-validation, holding out both seed sets:

```
female recall  98.79%      male recall  100.00%
male   -> FEMALE errors:  0 / 603
female -> MALE   errors:  0 / 1737
```

Zero measured false positives in both directions. Two caveats: this is zero on held-out
*seeds*, which are title-bout fighters and better connected than average; and every error
found during development was **bad ground truth, not a bad prediction** -- five "male
controls" who were women (Dern, Jandiroba, Robertson, Montano, Modafferi), then three
more from the AFBC mislabel. The method was right each time; the labels were wrong.

Sanity check that matters: UFC `Flyweight Championship` splits **14 women's / 29 men's**,
matching an independent fighter-continuity lineage analysis exactly.

### Result

```
30,666 women | 336,186 men | 366,852 labelled corpus-wide
within the giant component: 99.41% labelled (2,187 unlabelled of 368,099)
```

Title bouts identifiable as women's rise from **1,655 to ~6,500**, which is what makes
men's/women's belt separation possible (see the belt-count note below).

---

## 4. The remaining 2,187, and what 100% costs

Every unlabelled fighter is in a component containing **no seed at all** -- there are
zero cases of the algorithm reaching a fighter and failing to decide. The 2,187 inside
the giant component are there because the giant component is measured on *all* edges
while propagation walks a graph with `modified_rules` edges removed; cutting those edges
shears off pockets whose only bridge to the main body was such a bout.

They span 1,797 clusters:

| clusters | situation | |
|---|---|---|
| 234 | >=2 `modified_rules` links, unanimous | self-resolve, safe |
| 1,195 | exactly one link | ~1.9% would be wrong |
| 362 | no link to any labelled fighter | needs a seed |
| 6 | links disagree | needs verification |

- **1,563 ground truths -> 100% at 0 FP.**
- **362 ground truths -> ~99.99%**, accepting ~23 wrong labels from single-link clusters.
- **0 ground truths -> exclude them**  <- CHOSEN, see the cost table in section 2.

Efficiency is poor either way (~1.4 fighters per label), because the gap is not one blind
spot but ~1,800 fragments, 1,588 of them a single fighter.

### A second graph: the belt

Propagation walks the **opponent** graph, and by the count above every fighter it leaves
unlabelled sits in a component with no seed -- so more propagation on that graph cannot
help. A *title* bout also sits on a **belt**, which that method never walked.

A belt does not change gender. So an unlabelled title bout at an
`(owner, division, sport)` where every labelled bout is one gender is that gender.
`belts.py` fills it before the belt key is formed, guarded to a **single** gendered
sibling -- a promotion running both a men's and a women's belt at the same division is
left alone rather than guessed at.

```
723 belts had gender None (869 bouts)
  217 one gendered sibling   -> filled          258 bouts
   50 both M and F siblings  -> left alone
  456 no gendered sibling    -> nothing to infer from
```

The two directions reinforce each other, so they are alternated to convergence: a belt
lends its gender to an unlabelled bout, that bout lends it to its fighters, and those
fighters lend it to their other bouts. **259 bouts and 581 fighters** recovered.

Filling also **merges** the orphan into the real belt instead of leaving a one-bout
fragment beside it: 20,937 belts -> 20,720. In the title-fight ledger, unknown gender falls
from 911 rows to 633 (0.94%). The remaining 633 are bouts where neither fighter is labelled,
the belt has no labelled sibling, and neither fighter appears on a labelled belt anywhere --
there is nothing left in the data to read.

### The residual is excluded, not carried

Section 2 already drops gender-unlabelled fighters from the canonical modelling population.
`build_title_fight_viewer.py -> drop_ungendered()` makes the ledger agree with that instead
of carrying rows the population does not contain:

```
630 rows dropped      every one with no gender at all
1,179 fighters dropped   ungendered in every bout they appear in
66,910 rows remain    0 unknown gender, 0 unknown sport
```

Only the **ungendered rows** go, and a fighter is excluded only if he is ungendered
*everywhere*. A first version dropped the fighter whole -- if any of his title fights had
no gender, all of them went -- on the reasoning that he should not appear under a gender
only half known. That reasoning was wrong: a fighter who appears in one gendered bout has a
known gender, so his other bouts are known too. All 14 rows that rule discarded were
confidently male.

The bug it was papering over was real, though. The belt/fighter convergence above ran
inside `belts.py` and its results lived only in that function's memory, so every other
consumer -- the cancelled-bout path most visibly -- kept reading the stale
`fighter_gender.jsonl` and reported "unknown" for a fighter the belt graph had just
settled. It is now written to `data/fighter_gender_belt.jsonl` (581 fighters,
`gender_source: "belt"`) and merged wherever gender is read.

Quarantined to `data/removed_ungendered/` -- `title_fights.jsonl` and `fighters.txt` --
in the same spirit as `remove_cancelled_events.py`, so any of it can be reinstated.

---

## 5. Why this matters: belts

A belt is **not** identified by (promotion, weight, gender):

- **Promotion is the promoter, not the sanctioning body.** `WBA World Welterweight Title`
  was staged by 12 different promotions. One UK promoter at 135 lbs stages 52 distinct
  belts (British, Commonwealth, EBU European, WBC Silver, BBBC Area...).
- Outside boxing the reverse holds: `AFC Featherweight Championship` appears under 20
  unrelated promotions that all abbreviate to AFC.
- **Weight class names are not stable** -- KOTC calls its 145 lb division "Bantamweight".
- One bout can contest several belts: `EBU European & WBC Silver Super Lightweight
  Titles` (770 such strings over 965 bouts).

Counting with a sport-appropriate key -- string alone for boxing, (promotion, string)
elsewhere -- gives **~20,600 distinct belts** (18,500 grouping by `organisation`). Treat
it as a floor: 2,426 non-boxing title bouts have no `promotion_id` and collapse into one
bucket, and gender is not yet folded in.
