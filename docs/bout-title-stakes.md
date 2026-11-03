# What a title bout is worth

> Related: `belts.md` (how a belt is keyed) · `belt-lineage.md` (who held it) ·
> `weigh-ins.md` (what the fighters scaled at) · `title-belt-overrides.md` (the one
> outside-source file) · `stage3b-title-bouts.md` (where the bout pages come from)

A lineage answers *who was champion*. Elo needs the other question: **what could each
fighter win or lose in this bout**. Those come apart, and the cases that separate them are
exactly the ones a lineage gets wrong.

`derive/bout_title_stakes.py` -> `data/bout_title_stakes.jsonl`. ~10 s, no network.
One row per **(bout, belt)** -- a unification yields one row per belt, because the stakes
differ per belt. An interim title is the same belt (see belts.py), so it does not add a row.

## The four cases, side by side

| | roles | can_win | can_retain | can_lose | `stake` | belt certainly moves |
|---|---|---|---|---|---|---|
| **Johnson** UFC 227 | challenger / champion | T / F | F / **T** | F / T | `defence` | no |
| **Jones** UFC 232, 285 | vacant / vacant | T / T | F / F | F / F | `vacant_open` | yes |
| **Oliveira** UFC 274 | champion / challenger | F / T | **F** / F | T / F | `champion_cannot_retain` | yes |
| **Romero** UFC 221 | vacant / vacant | **F** / T | F / F | F / F | `vacant_one_way` | no |

**`can_retain` is the axis that does the work.** Without it Oliveira and Johnson are
identical rows -- champion on one side, challenger on the other, one belt in play. Oliveira
missed weight, so the belt was leaving him whichever way the fight went; Johnson's was the
most defended belt in the promotion's history. Same shape, opposite meaning.

Romero is the mirror. Both men are listed `Vacant`, but only Rockhold could pick the belt
up, so a Romero win settled nothing -- which is why the interim title stayed vacant and why
crowning him is the classic lineage error.

## Browsing it

```bash
python3 scripts/build_title_fight_viewer.py   # pack viewer/data/title_fights.json
python3 viewer/serve.py                       # http://localhost:8731
```

`viewer/index.html` filters all 59,430 rows by stake, **outcome**, **level**
(pro/amateur), evidence tier, type, gender, sport, org, division, **weight**, year and
thresholds.

**Organisations lead with their acronym** — `(UFC) Ultimate Fighting Championship`, from
`promotions_detail.jsonl -> acronyms` (first entry; 3,531 of the 3,790 promotions in the
ledger have one, and the 15 whose acronym is just the name again are left bare). The Org
column truncates, so `(UFC) Ultimate Fig...` stays identifiable where the bare name does
not, and the filter box accepts the acronym on its own when it names exactly one org.

The organisation list is **A–Z until a sport is picked, then ranked by bouts in that
sport** — with a sport chosen, "who runs this" is a ranking question. The counts are
already scoped to every other active filter, so the ranking follows the rest of the
selection.

**Click a fighter's name** in the Result column to see only their title fights, wins and
losses alike; a chip appears next to the row count, and clicking the same name again (or
the chip's ×) clears it. It composes with every other filter, so it answers "Volkanovski's
title fights that were interim" directly.

That filter keys on `fighter_slug`, **not** on the displayed name, and the packed viewer
data carries the slugs in `cols.as` / `cols.bs` against `dicts.fid` for exactly that
reason: **1,028 names in this corpus belong to more than one fighter** — eight different
men are called "Pedro Henrique". Searching that text finds 16 title fights across all of
them; clicking one name finds the 3 that are actually his. The slug is the whole string,
never just its leading number. Carrying the two extra columns took the packed file from
7.0 MB to 9.3 MB, which is a local-only load.

**Copy as text** emits TSV with a header row, so a paste into Google Sheets lands in
columns; *Copy & open a new Sheet* does the copy and opens a blank sheet in a new tab to
paste into. (A local page cannot write to a Sheet directly -- that needs an API key and an
OAuth grant -- so the paste is yours to make.) Everything the table formats for a human
goes out raw, because `1.4y` and `155 lb` cannot be sorted or charted:

```
Org, Sport, Level, Division, Weight lb, Gender, Type, Stake, Outcome, Defences before,
Reign days, Bout # on belt, Belt age days, Belts, Unification, Winner, Verb, Opponent, Date
```

Weight is a number, the two durations are in days, `Unification` is `TRUE`/`FALSE`, and the
result is split into `Winner` / `Verb` / `Opponent` rather than one `X def. Y` string.
Scheduled bouts are excluded by
default — the corpus runs months ahead of today, so "newest first" would otherwise open on
cards that have no result yet.

**The filters are faceted.** Every option carries the number of rows it would yield given
the other filters, and an option that would yield none is disabled: pick MMA and the 4,158
organisations fall to 2,483; pick the UFC and the 233 divisions fall to its 12 while 21 of
the 22 sports go dead. Counts for a facet are computed with that facet's own selection
lifted — otherwise an unticked box would read zero forever and could never be re-ticked —
and a ticked option that has become unreachable is dimmed rather than disabled, so a choice
can always be undone. Typing an organisation or division that no other filter can reach is
rejected outright instead of returning an empty table.

## Cancelled title fights

A title fight that was called off is not in `bouts.jsonl` at all -- Tapology lists it only
in a "Cancelled & Fizzled Bouts" block on the event page. The stage-3 parser captured that
block into `event_details.jsonl -> cancelled_bouts` with the status and the reason, but
**not** whether it was for a title: that is a championship-belt SVG flanking the status
pill, with no text anywhere in the row.

`derive/cancelled_bouts.py` -> `data/cancelled_title_bouts.jsonl` recovers it from
`data/html/` with no crawl --
`id='sectionCancelled'`, rows keyed `id='bout<ID>Cancelled'`, and
`<svg class="... fill-tap_darkgold">` present twice on a title bout and not at all
otherwise. UFC 189 is the check: Aldo vs McGregor scores 2 belts, the other five cancelled
bouts on that card score 0.

```
39,565  events with a cancelled or fizzled bout
 8,101  of those bouts were for a title   (7,978 cancelled, 123 fizzled)
 2,501  carry a reason -- "Aldo Rib Injury", "Tsarukyan Injury", "Tau Missed Weight"
   147  are UFC
```

**The 8,101 bout pages were then crawled** -- 210.6 min at 37.9 bouts/min, 0 anomalies,
0 misses, 0 parser errors. Tapology keeps a bout page for a fight that never happened, and
it still states the title on the line, so the crawl returned far more than expected:

```
8,101  pages, 100% recovered
8,101  with the title on the line   7,679 divisional + 422 tournament finals
7,600  with a contracted weight
4,664  with a belt status before the fight   2,347 vacant / 2,317 held
```

That last line is the useful one: a cancelled title fight still says whether a champion was
due to defend (`cancelled_defence`) or the belt was already vacant (`cancelled_vacant`).
UFC 189 reads `UFC Featherweight Championship / featherweight / 145 lb / Held by Aldo /
"Aldo Rib Injury"`.

Weight, **sport** and **Pro/Am level** are parsed from the archived HTML rather than the
parsed row. Weight and level the stage-3b extractor never captured at all. Sport it does
capture -- but from the *worklist row*, and a cancelled bout has no worklist row, so all
8,101 came back `None`. The page states it twice (`<title>… | MMA Bout | Tapology`, and the
`Cancelled MMA Bout` pill); checked against every non-MMA sport in the corpus -- Boxing,
Bare Knuckle, Muay Thai, Kickboxing, Grappling, Lethwei, Sanda, Pancrase and the rest --
the page agrees with the independently derived `sport` every time.

```
8,101  sport   all MMA, as it happens
8,101  Pro/Am  4,025 Professional / 4,076 Amateur
7,592  weight
```

A tenth source is `data/removed_cancelled/` -- 3,263 events cancelled outright and
quarantined so they cannot crown anyone in a lineage. Only 10 have a scraped card, and
those 10 do carry a weight badge.

Cancelled rows never touch `belts.jsonl` or `belt_reigns.jsonl`. A fight that did not happen
must not move a belt.

## Weight audit

`scripts/weight_audit.py` -> `analysis/out/weight_audit.txt`.

**Every weight in the ledger is an integer** -- there are no fractional values, so nothing
is lost to rounding at rest. The spread is the two standard ladders laid on top of each
other: MMA (135/155/170/145/125/185/265/205/115) and boxing (154/147/140/160/168/175/122).

```
                 n       min    p1   p25   med   p75   p99    max
Professional  39,509       2    105   132   147   170   265   1102
Amateur       24,352       2    115   135   155   170   265    882
```

Amateur sits ~8 lb heavier at the median, which is the boxing ladder's lighter classes
being overwhelmingly professional.

### Missing: 817 of the completed rows

```
817 completed rows have no weight at all   610 pro / 204 am / 3 youth
     MMA 241 | Bare Knuckle 191 | Grappling 130 | Boxing 109 | Kickboxing 57 | Muay Thai 55
1,596 more are tagged HW -- unlimited, which is a weight class, not a gap
```

Bare-knuckle and grappling are over-represented: those cards often print no weight at all.

### Unlimited is a class, not a number

An uncapped title is written two ways -- the badge `HW`, and a round kg cap converted to
pounds: 441 / 661 / 882 / 1102 lb are exactly 200 / 300 / 400 / 500 kg. Both now record as
**`openweight` with no number** (1,653 rows) instead of a 1,102 lb fight that would sit at
the top of every weight sort. `unlimited` is a boolean on the stakes row.

Normalisation runs over every row in `build_title_fight_viewer.py`, not on the main path
only: the cancelled rows carry their own weight extraction, and four sentinels survived
into the ledger before that was centralised.

### The 2 lb fight, and the light tail

Ten rows carried a weight under 45 lb, below any adult or youth class. They are Tapology
badges that latched onto something else -- the 2 lb "NBCSP Championship" has
`round_format: 5 x 2`, and three 11 lb rows are also `5 x 2`. The number is a round length,
not a weight, so it is no longer kept as one; those rows read blank.

What remains under 100 lb is real: Japanese kickboxing at 45-47 kg (K-1 Japan, NJKF,
DEEP KICK), converting correctly to 99-104 lb.

**The light tail is mostly real.** Below 100 lb is dominated by Japanese kickboxing at
45-47 kg (K-1 Japan, NJKF, DEEP KICK), which converts correctly to 99-104 lb. Only 11 rows
are genuinely wrong: 5 where a named division carries an impossible number (a 15 lb
welterweight), 3 comma-decimal parses, and 3 one-offs.

### A comma decimal was being read as the whole weight

`Imperio Muay Thai 63,5kg Championship` -- a Brazilian comma decimal. The division regex's
`\b` landed *after* the comma and matched `5kg`, so a 63.5 kg title was keyed as 5 kg.
Fixed in `belts.py` (`NUMW` accepts `[.,]`, `canon_weight` normalises it); 5 titles
affected, now keyed `63.5kg` / `55.5kg`.

### The weight badge is trustworthy

3,436 title strings state an explicit weight, which can be checked against Tapology's
badge independently:

```
3,394  agree  (98.8%)
   42  differ -- and 39 of those are the badge being RIGHT:
          "93+ kg" means uncapped above 93 kg, badge 265 = the heavyweight cap
          "Bushido 63.5 LBS Championship" says LBS but means kg; badge 140 is correct
    3  the badge is genuinely wrong -- the comma-decimal titles, badge 11 lb
```

Three bad badges in 3,436 checks. No repair rule is applied: at that rate a rule would
create more errors than it fixes, and the "63.5 LBS" case shows the title is not always
the more reliable side.

## A page-level field cannot describe a unification

`Belt Status Before Fight` and `comparison.title` are **one value for the whole bout**, but
a unification puts several belts on the line with different statuses. Two failure modes,
both now guarded:

**The holder of one belt is credited with all of them.** Thibeault vs Robinson 2026 is
billed `Ring Magazine, IBF World & WBO World Female Middleweight Titles` and reads
"Held by Robinson" -- true of the IBF and WBO belts she held, false of the Ring title, which
had been vacant since Shields left it. A holder cannot be defending a belt whose first bout
in the corpus **is** this one, so the page status is refused there: **460 page-status and
271 stated-role refusals**.

**A split-holder unification.** At Shields vs Hammer 2019 the belts were split -- Shields
held WBA, WBC and IBF, Hammer held WBO -- and the page says only "Held by Shields". The
evidence that settles it is each belt's OWN previous bout: WBA/WBC/IBF were last won by
Shields, WBO by Hammer, and both are already in the corpus. So for champ-vs-champ the
derived champion beats the recorded one, because recorded is not per-belt and derived is:
**534 rows settled by the belt's own last bout**, 206 falling back to the page, 11
unresolved.

## The complete `stake` vocabulary

Six values, and that is all of them -- every one is derived from the three per-side axes,
so the list is closed rather than a growing set of special cases.

| `stake` | rows | | what it means |
|---|---:|---|---|
| `vacant_open` | 39,612 | 66.65% | nobody holds it, either man can leave with it |
| `defence` | 19,710 | 33.17% | champion can retain or lose; it can change hands |
| `vacant_one_way` | 57 | 0.10% | nobody holds it and only ONE man can win it -- **Romero** |
| `champion_cannot_retain` | 30 | 0.05% | champion stripped on the scale; the belt leaves him either way -- **Oliveira** |
| `challenger_cannot_win` | 19 | 0.03% | challenger missed weight; the champion can still lose the belt, but to *vacancy*, not to his opponent |
| `champion_cannot_lose` | 3 | 0.005% | opponent missed weight; the champion keeps it whatever happens |

The bottom four are 109 rows out of 59,787 -- 0.18%. They are rare and they are exactly the
rows a naive "was this a title fight" flag gets wrong, in both directions.

`belt_certainly_moves` is a separate boolean: true when the belt is guaranteed to end up
with someone other than its current holder (every `vacant_open`, every
`champion_cannot_retain`). 39,642 rows, 66.70%.

### Role pairs behind them

| pair | rows | |
|---|---:|---|
| `vacant / vacant` | 39,200 | no holder |
| `challenger / champion` | 18,800 | ordinary defence |
| `champion / champion` | 535 | champ-vs-champ; the recorded holder decides who holds *this* belt |
| `challenger / challenger` | 404 | vacant, but Tapology labels both as challengers |
| `challenger / interim_champion` | 213 | interim champion defending |
| `champion / interim_champion` | 185 | unification |
| `champion / vacant`, `challenger / vacant`, `interim_champion / vacant` | 93 | mixed labels, treated by whichever side holds |

A unification is **not** a `stake` value and never will be: `stake` is derived mechanically
from the three per-side axes and answers only *what happens to this one belt*. In
Gaethje vs Topuria the undisputed belt is being defended -- Topuria can retain it, Gaethje
can win it -- so the stake is `defence` and the `unification` flag on the same row carries
the rest. Read the two together.

`unification` (champion vs champion, champion vs interim champion, or more than one belt
linked to the bout) is true on 2,373 rows, 3.99%. `belts_on_the_line` runs 1 to 5 --
57,219 rows put one belt up, 1,574 put two, and 637 put three or more.

## `stake` is before the bout, `outcome` is after it

They answer different questions, so keep them apart. On **5,826 rows** the stake is
`defence` and the belt changed hands anyway -- a champion walked in and a new champion
walked out.

`stake` is what *could* happen, fixed before anyone walks out: it reads `defence` for every
bout a champion starts, including the ones he loses. That is the right shape for a rating
model, which has to price the fight from what was on the line, not from how it went.

`outcome` is what *did* happen. **A man who did not hold the belt walking in and leaves
with it has WON the title; only a man who already held it and kept it has DEFENDED it.**

| `outcome` | rows | | |
|---|---:|---|---|
| `title_win` | 44,423 | 74.75% | the winner did not hold it going in and leaves with it |
| `defence` | 13,343 | 22.45% | the winner held it going in and kept it |
| `still_vacant` | 695 | 1.17% | nobody held it and nobody could leave with it |
| `retained` | 484 | 0.81% | the champion kept it without winning -- a draw, or a no-contest |
| `null` | 460 | 0.77% | not fought yet, or no result recorded |
| `vacated` | 25 | 0.04% | it left its holder without reaching his opponent |

The UFC featherweight belt is the whole argument in one column:

```
2014-10-25  defence      defence     José Aldo def. Chad Mendes
2015-07-11  vacant_open  title_win   Conor McGregor def. Chad Mendes      (interim)
2015-12-12  defence      title_win   Conor McGregor def. José Aldo
2017-06-03  defence      title_win   Max Holloway def. José Aldo
2017-12-02  defence      defence     Max Holloway def. José Aldo
2019-12-14  defence      title_win   Alexander Volkanovski def. Max Holloway
```

Aldo–Mendes and the Holloway–Aldo rematch are defences. Everything else on that list is a
man taking a belt off someone, and the `stake` column calls all of them `defence` because
before the bell that is exactly what each of them was.

`vacated` is the odd one and it is worth knowing why it exists: a champion stripped on the
scale who then wins (the belt had already left him), or a champion beaten by an opponent
who missed weight and so could not take it off him. 25 rows.

## Where the labels come from

Tapology states the per-side role outright in `comparison.title`, a field nothing in the
pipeline used before. The whole vocabulary is ten values:

```
41,160  Vacant                              57  Vacant Ineligible to Win Belt
16,450  Challenger                          26  Champion Ineligible to Win Belt
16,266  Champion                            18  Challenger Ineligible to Win Belt
   413  Interim Champion                     3  Champion Ineligible to Lose Belt
```

`Ineligible to Win` on a **champion** means he cannot RETAIN -- he was stripped on the
scale. `Ineligible to Lose` is its mirror: the opponent missed weight, so the champion
keeps it whatever happens.

## `roles_source` is a confidence tier, not a footnote

```
stated        37,389  62.9%   Tapology's own per-side row
title_string   1,324   2.2%   the card names the bout "Vacant ... Championship"
page_status      194   0.3%   `Belt Status Before Fight` and nothing better
lineage       20,523  34.5%   our own reconstruction
```

`comparison.title` and `Belt Status Before Fight` are independent, and they **never**
contradict each other on vacancy -- zero rows in 58,201 where one says vacant and the other
names a holder. Both are direct statements and are treated as such.

The `lineage` rung is not. Scored against the 37,390 bouts where Tapology states the roles,
our reconstruction reproduces them **83.9%** of the time (85.3% if you score only which
side holds the belt, which is the axis the stakes actually use):

```
 4,191   champion/challenger    read as  vacant/vacant
   710   champion/challenger    sides swapped
   585   vacant/vacant          read as  champion/challenger
   523   interim / champ-vs-champ label mismatches
```

It used to be 60.2%, and the failure used to run the other way -- 10,494 vacant titles read
as a champion defending. Almost all of that was one off-by-one: a reign is matched when
`won_at_date <= event_date`, so the reign *won at the bout being classified* matched it,
and the winner was credited as the champion going in. Every vacant title fight became a
defence with a 0-day reign and 0 defences behind it. There was already a guard for the
reign that *ends* at a bout; the mirror guard for the one that *begins* at it was missing.
Adding it moved 8,063 rows from `defence` to `vacant_open` and touched no `stated`,
`page_status` or `title_string` row.

What remains runs in the conservative direction -- a real champion read as vacant, rather
than a champion invented. That is still a belt vacated between fights, which a
bout-bounded lineage cannot see. **Weight or gate on `roles_source`.** A `lineage` row is
a guess, and a third of the corpus rests on it.

## Amateur or professional

`level` is `Professional` / `Amateur` / `Juniors` / `Kids`, taken straight from the bout
page and complete on every row:

```
38,260  Professional        88  Juniors
21,080  Amateur              2  Kids
```

**35% of the ledger is amateur.** Without this a regional amateur league's belt and a UFC
belt are both just "a title fight" — and the effect on any ranking is not subtle. The
busiest MMA organisation in the corpus is an amateur one:

```
MMA, any level                        MMA, professional only
  655  unknown                          408  (UFC) Ultimate Fighting Championship
  649  (ALMMA) Amatorska Liga MMA       339  (KOTC) King of the Cage
  448  (KOTC) King of the Cage          305  unknown
  408  (UFC) Ultimate Fighting Ch…      200  (CFFC) Cage Fury Fighting Championships
```

It is trustworthy: it agrees with the sport string's own prefix (`Amateur MMA`,
`Professional Boxing`) on all 58,111 rows where both are decisive, with **zero**
disagreements. Cancelled bouts carry none — Tapology prints no level for a fight that never
happened — so they read `—` in the viewer.

## Weight, because the division NAME lies

`weight_lb` and `weight_label` carry the weight actually contested, from Tapology's
`weight_badge` -- 56,922 rows numeric, 1,631 `HW` (unlimited), 877 unknown.

It is needed because promotions rename divisions without moving them:

```
2001-02-23  UFC Bantamweight Championship   155 lb   Jens Pulver def. Caol Uno
2011-07-02  UFC Bantamweight Championship   135 lb   Dominick Cruz def. Urijah Faber
1998-10-16  UFC Lightweight Championship    170 lb   Pat Miletich def. Mikey Burnett
2026-06-14  UFC Lightweight Championship    155 lb   Justin Gaethje def. Ilia Topuria
```

By name, Pulver belongs with Cruz. By weight he belongs with Khabib -- and filtering the
UFC to 155 lb gives 46 bouts running unbroken from Pulver's 2001 win through Penn, Sherk,
Edgar and Henderson to Khabib and Gaethje. **Group by weight, not by division name.** The
name is still carried, because it is what the promotion called it that night.

## Scale fields

Stakes say whether the belt moves; these say how much the belt is worth. They were briefly
squashed into one composite score; they are kept separate because the composite hid which
part was doing the work.

| field | |
|---|---|
| `champion_defenses_before` | successful defences in the current reign. Johnson at UFC 227: **11** -- the promotion record, correct to the fight. |
| `champion_reign_days` | how long he has held it. Follows `continued_from`, so a reign carried across a rename or an era split is not reset to nought -- Pulver's September 2001 defence reads 217 days, not 0. |
| `belt_bouts_before` | title bouts this belt has seen, ever |
| `belt_age_days` | days since the belt's first title bout |
| `belts_on_the_line` | >1 is a genuine two-belt unification |
| `unification` | champion vs champion, or champion vs interim champion |
| `interim` | this link is the interim title |

`belt_bouts_before` and `belt_age_days` separate a first-ever regional title from a UFC
belt with 47 bouts and 25 years behind it, without any promotion-strength model.

## Known limits

- **34% of rows are `lineage`-sourced and 84% accurate.** This is the headline caveat.
  The residual error now runs the safe way -- a real champion read as vacant, rather than a
  champion invented -- but a third of the corpus is still a reconstruction.
- **34 champ-vs-champ bouts cannot be resolved to a holder.** Both sides read `Champion`
  and the recorded holder matches neither, so neither is credited with holding *this* belt.
- **`champion_defenses_before` follows our lineage**, so it inherits the same blind spot:
  a reign that silently absorbed a vacancy will over-count.
- **`outcome` is only as good as the recorded result.** 460 rows have no result at all
  (scheduled, or never recorded) and carry `null`.
