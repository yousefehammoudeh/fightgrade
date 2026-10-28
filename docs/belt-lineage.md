# Belt lineage

> Related: `belts.md` · `bout-title-stakes.md` · `stage3b-title-bouts.md`

Who held what, when, and how each reign began and ended.

`scrapers/derive/belt_lineage.py` -> `data/belt_reigns.jsonl`, `logs/lineage_audit.log`.
~8 s, no network. Depends on `belts.py` (see `belts.md`) and the stage-3b crawl.

## Two independent signals

| | |
|---|---|
| **RECORDED** | `Belt Status Before Fight` from the bout page -- Tapology's own statement of who held the belt going in. `Held by <name>` / `Vacant` / `N/A`. |
| **DERIVED** | the winner of the previous bout for the same belt. |

Where both exist they should agree. **Recorded wins** when they conflict: it is a direct
statement, where derived is an inference that assumes we hold every intervening title
fight. Disagreements are reported, never silently resolved.

`Held by Liston` is a surname, resolved against the two fighters in the bout --
**14,538 of 14,539 recorded holders are one of the two**, so resolution is effectively
free.

## Results

```
reigns reconstructed 50,543      belts with a lineage  9,404
                                 single-bout belts    11,300
reigns by evidence: derived 39,784 | recorded 10,716 | uncrawled 43
```

A draw or no-contest leaves the champion in place. `Vacant` opens a new reign regardless
of who won last.

## Five rules about when the belt does NOT move

**A vacant title won twice by the same fighter was not awarded the first time.** You cannot
contest a vacant title you already hold, so when the same man wins the same belt in two
consecutive vacant-title bouts with no defence between, the first bout awarded nothing --
he missed weight. Tapology usually says `Ineligible to Win Belt`; at UFC Fight Night 169 it
records a plain `Vacant`/`Vacant`, which crowned Deiveson Figueiredo five months early.
Bounded at 200 days (`UNAWARDED_MAX_GAP`): a longer gap is a belt that lapsed and was
genuinely re-won, which is two real reigns. **56 reigns dropped.**


**A winner who missed weight does not take the belt.** Tapology marks them
`Ineligible to Win Belt` in the per-side `comparison.title` row -- 105 bouts corpus-wide,
54 of them won by the ineligible fighter. Yoel Romero beat Luke Rockhold at UFC 221 and the
interim title stayed vacant; without this the lineage crowns him. Same for Topuria at
Cage Warriors 94, Koike at RIZIN 43 and Piersma at LFA 233. When it is the *champion* who
missed weight the reign ends there -- that is Charles Oliveira at UFC 274.

**A card that says "Vacant" is a statement of status.** It agrees with the crawled
`Belt Status Before Fight` 7,445 times against 79 conflicts, so where the page carries no
status at all (1,093 bouts) the card is taken as the vacancy. `held` still wins: it is the
more specific statement.

**A champion who is not in the bout is not defending.** With no recorded status, a title
bout the derived champion is not fighting in cannot be a defence -- he vacated, or our
chain is stale. Either way the winner beat nobody, so the reign opens `vacant`. This is
most of ACA's and LFA's vacated titles, where the card says only
`ACA Welterweight Championship`.

**An inaugural bout has no champion to defeat.** Nothing derived and no recorded holder
means the belt was not being defended: Frank Shamrock at UFC 16, every PFL lineal belt, the
first bout of most RIZIN and KSW divisions.

## Uncrawled bouts still build a reign

The stage-3b worklist only queued belts with **>= 2 bouts**, so a belt whose entire life is
one title fight was never crawled and produced no reign at all -- that is every PFL lineal
belt, and 10,580 belts overall. Those bouts are admitted from the card alone, with
`Vacant`/`Inaugural` in the title as the status signal and `evidence: "uncrawled"` marking
them.

**The gap crawls have since closed almost all of it**: HTML coverage of the ledger is 99.9%,
so `uncrawled` has fallen from 10,928 reigns to **43**, and `recorded` has risen from 9,401
to 10,716. The fallback stays because the 48 bouts still missing need it.

### Worked example -- UFC men's flyweight

```
2012-09-22  Demetrious Johnson   vacant              12 defenses  lost 2018-08-04
2018-08-04  Henry Cejudo         defeated_champion    1 defense   lost 2020-02-29
2020-02-29  Deiveson Figueiredo  vacant               0           lost 2020-07-18
2020-07-18  Deiveson Figueiredo  vacant               3           lost 2021-06-12
2021-06-12  Brandon Moreno       defeated_champion    1           lost 2022-01-22
2022-01-22  Deiveson Figueiredo  defeated_champion    1           lost 2023-01-21
2023-01-21  Brandon Moreno       defeated_champion    1           lost 2023-07-08
2023-07-08  Alexandre Pantoja    defeated_champion    5           lost 2025-12-06
2025-12-06  Joshua Van           defeated_champion    1           current
```

Matches the real history. The two adjacent Figueiredo reigns in 2020 are faithful to the
source, not a bug: he missed weight in February so the belt was not awarded, and the July
rematch was again for a vacant title. Tapology marks both `Vacant` and the model follows.

## The disagreement rate is a COMPLETENESS measure

Overall agreement is 10,936 vs 3,306 -- **23.2% disagreement**, which looks alarming until
it is broken down by the gap between consecutive bouts we hold for that belt:

```
gap to previous bout       agree  disagree  disagree %
<90d                       1,219       770       38.7%
90-365d                    8,160     1,276       13.5%
1-2y                       1,296       544       29.6%
2-5y                         246       488       66.5%
5y+                           15       228       93.8%
```

It scales almost perfectly with the gap. **A 5-year gap means the belt changed hands in
fights this corpus does not contain**, so derived is stale and disagrees. The 90-365 day
bucket -- a normal defence cadence, and the bulk of the checks -- is the real error rate
at **13.5%**.

**`<90d` is the exception and points the other way.** Two title fights for one belt inside
90 days usually means the key merged two genuinely different belts. That bucket is a
belt-key quality signal, not a lineage one.

Use `gap_to_previous` on each reign to filter. A reign with `evidence: "recorded"` and a
sub-year gap is solid; `evidence: "derived"` after a 5-year gap is a guess.

## Other counters

- **`vacant despite a previous winner`: 20,603.** The belt was vacated, stripped, or an
  interim was promoted between bouts. Real events, not errors -- and precisely what
  derived-only reconstruction cannot see.
- **`status absent`: 11,484.** Tapology records `N/A` and neither vacancy rule fires.
- **`recorded holder, nothing derived yet`: 2,563.** First bout we hold for that belt.

## Reign continuation

A reign that begins with no bout usually **continues one that just ended**. Linked only on
tight evidence -- the loose version mostly finds fighters who held two belts at once.

**The discriminator is that the predecessor belt must be DEAD.** A predecessor still holding
bouts afterwards means the fighter held two belts, not that one became the other. Without
this the detector finds 5,049 candidates; with it, 959.

```
division_renamed    859     belt renamed, weights within 10 lb
era_carryover       348     carried across a weight-era split
interim_promoted    203     interim champion elevated
promotion_absorbed    7     inherited when the parent absorbed another promotion
                  4,443     still unlinked
```

Each linked reign carries `continued_from` naming the predecessor belt, the kind, the true
start date and the gap, so the link is inspectable rather than a silent rewrite.

Two details that make `promotion_absorbed` work:

- **Ownership is a SET, not the modal value.** Strikeforce's most common owner is
  `Forza, LLC`; only 1 of its 78 events is tagged `Zuffa, LLC`. Matching on the modal owner
  never links Strikeforce to the UFC. Promotions are related if they share **any** ownership.
- **Single-bout belts still build a reign.** They have no lineage, but a later belt may
  continue from them -- Jens Pulver's UFC 30 win and Mark Coleman's Superfight title are
  both single-bout belts.

## Validation

`scripts/verify_lineage_textfile.py` checks every reign in
`combat_sports_title_lineages.txt` against ours across its 10 promotions, treating the file
as a **floor**: the champion must be present and the manner of winning must agree. Dates are
not compared (the file dates a reign from the administrative award, we date it from the
first title bout we hold) and `promoted_from_interim` starts are excluded, since a
bout-bounded lineage cannot see a promotion that happened between fights.

```
ORG                    file    repro   manner  MISSING  skipped   extra
UFC                     173      166        2        0        5   4
Bellator                 73       69        2        2        0   6
ONE Championship         73       67        2        3        1  10
PFL                      10        9        1        0        0   1
RIZIN                    26       25        1        0        0   1
KSW                      53       49        1        0        3   3
Cage Warriors           112       90        1       21        0  34
LFA                     116      105        3        2        6   7
Invicta FC               39       35        0        2        2   2
ACA                      72       63        4        1        4   7
TOTAL                   747      678       17       31       21  75
```

**95.7% of the file is reproduced**, and 21 of the 31 misses are pre-2011 Cage Warriors,
where Tapology tags no title on any of the 226 bouts across those 24 events -- a source
gap, not a derivation one. Full per-division output: `analysis/out/lineage_vs_textfile.txt`.

### UFC: all 12 divisions reproduced, six rows still differ

Nothing is missing. What remains is two modelling choices and one source error:

- **Superfight is its own belt** (3 rows). The file folds Ken Shamrock's openweight title
  into Heavyweight and calls Coleman's start a `tournament_final`. We keep the two belts
  distinct and join them with `continued_from: division_renamed`, which is why Coleman
  appears once on each.
- **A 2001 rename shows on both belts** (3 rows: Ortiz, Pulver, Miletich). The file records
  one reign under the name in use when it was won; we record the original plus a
  continuation row carrying `continued_from`, so the join is inspectable.
- **Frank Mir at UFC 48** is a Tapology error, not ours. Tim Sylvia was stripped in October
  2003 and the bout was for the vacant title, but the page states `Held by Sylvia`. We
  follow the recorded holder; overriding an explicit `Held by` is not something the source
  supports.

## Known limits

- **Reigns are bounded by our data, not by history.** `won_at_date` is the first bout we
  hold, which for a long-running belt may be years into a reign.
- **An administrative vacancy between fights is invisible.** Tapology's card says only
  `ACA Welterweight Championship`, so a belt vacated and re-awarded reads as one reign
  unless the champion is absent from the next title bout. This is what the file does better
  than we do, and it is most of the 19 remaining manner disagreements.
- **ONE Light Heavyweight and ONE Heavyweight share a key.** 225 lb and 265 lb both convert
  to conventional heavyweight, so the two lineages cannot be separated from the string.
- **14 bouts are unreachable** -- dead Tapology slugs that 200-redirect to the homepage
  (`redirected` in `anomalies_bouts.jsonl`). Everything except belt status is still held
  from the event pages.
- **Reigns from `evidence: "uncrawled"` have no `Belt Status Before Fight`** -- only the
  card. Filter them out for the strictest view.
