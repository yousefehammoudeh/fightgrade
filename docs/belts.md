# Reconstructing belts from title bouts

> Related: `belt-lineage.md` · `bout-title-stakes.md` · `title-belt-overrides.md`

`title_bout` is free text on the card, not a belt id. These are the rules for turning
59,787 title-bout links into a belt registry, and the reason each rule exists.

Derived by `derive/belts.py` -> `data/belts.jsonl`, `data/bout_belts.jsonl`,
`logs/belt_worklist.txt`. Seconds, no network.

## Belt key

```
BELT = (owner, division, gender, era, sport)
```

| axis | rule |
|---|---|
| **owner** | The **sanctioning authority** parsed from the string when there is one -- all of boxing, plus ISKA/WAKO/WKN/WKA/WPMF/IFMA/WMC/IKF/WKF/WMF/WBC Muay Thai. Otherwise `promotion_id` plus any authority text. |
| **division** | The division **name**; the canonicalised weight only as a fallback; `openweight` when the title names no division at all. |
| **gender** | From `data/fighter_gender.jsonl`; the string's own `Women's` marker as fallback. A lone **propagated** `F` no longer overrules a known `M`. |
| **era** | Splits a belt whose weight shifts and **stays** shifted. |
| **sport** | A title contested under different rules is a different championship. |

### Sport: four belts, one name

ONE Championship awards a Bantamweight title in MMA, another in Muay Thai, another in
kickboxing and another in submission grappling. All four print as
`ONE Bantamweight Championship`, so without `sport` in the key they collapse into one belt
with four concurrent champions -- Rodtang and Demetrious Johnson in the same lineage.
**669 belts spanned more than one sport** before this; none do now.

### Openweight: a title that names no division

`UFC Superfight Championship` names no division and most of its bouts carry no weigh-in
badge, so all five of Ken Shamrock's Superfight defences were **dropped** as unparseable
and UFC 12 -- the one bout that happened to have a `265` badge -- was stranded alone on a
`265lb` key. A title with neither signal is now `openweight`, and when an owner never names
a division anywhere its badged bouts fold in too, so the belt is whole.

### ONE Championship's 2018 weight realignment

ONE raised every **men's** weight limit on 2018-01-01 and kept the division names. Tapology
names a division by its numeric limit, so from 2018 the same ONE belt appears one
conventional class **higher**: Aung La N Sang's ONE Middleweight title is
`ONE Middleweight Championship` in 2017 and `ONE Light Heavyweight Championship` in 2018.
Keyed on the name, every ONE men's belt split in two and collided with the neighbour below
it -- `bantamweight` held ONE Bantamweight until 2017 and ONE Flyweight after.

Post-realignment names are shifted back down one class (199 bouts). The women's ladder was
never realigned but Tapology converts it too, so the same shift applies to women with no
date gate. **Heavyweight is excluded**: ONE Light Heavyweight (225 lb) and ONE Heavyweight
(265 lb) both land on conventional heavyweight and cannot be separated from the string.

`interim` is **not** part of the key: an interim title is the same belt contested while the
undisputed champion is unavailable. It rides on the link and the lineage tracks the two
states concurrently (`belt-lineage.md`).

`Vacant` and `Inaugural` are **bout state, not belt identity** -- strip them. 19.6% of
title strings carry `Vacant`; keeping it roughly doubles the belt count.

### Era: a promotion redefines a division and keeps the name

The UFC's "Middleweight" was 199 lb until 2001 and 185 lb after; its "Lightweight" was
170 lb before 2001; its "Bantamweight" was 155 lb in 2001 and 135 lb from 2011. One key
covering both is nonsense -- it puts Frank Shamrock and Tito Ortiz in the same lineage as
Anderson Silva.

Detected from `weight_badge`, never hardcoded: cut where the weight moves more than 8 lb
(catchweight and weigh-in variance stay inside one division) and stays moved for at least
2 further bouts. Finds **exactly** the three UFC cases and leaves light heavyweight and
welterweight whole. Conservative corpus-wide: **50 cuts across 20,704 belts**, the rest
being Shooto and KOTC, which genuinely redefined divisions.

### Tournament finals that founded a lineage

`title_kind == "tournament"` rightly excludes bracket rounds, but early promotions crowned
their first champions in a tournament. UFC 16 crowned Pat Miletich; Bellator founded five
divisions this way; so did DREAM, Sengoku, DEEP, Pancrase and Road FC.

Admitted only on Tapology's own evidence: the tournament winner must be the recorded holder
at that belt's **first** title bout, same promotion and weight. That is a statement from the
source, not an inference about what a tournament meant. **57 finals admitted.**

### Owner: sanctioning body vs promotion

- **The promoter is not the sanctioning body.** `WBA World Welterweight Title` was staged
  by 12 different promotions; one UK promoter at 135 lb stages 52 distinct belts.
  Promotion-scoping mints one belt per promoter.
- **This is not boxing-only.** ISKA titles are staged by 31 different promotions, WBC Muay
  Thai by 18, WAKO by 14, WKN by 13. Same bug, smaller blast radius (7-8% of Muay Thai
  and kickboxing title bouts).
- **But the reverse holds for promotions' own belts.** `AFC Featherweight Championship`
  appears under 20 unrelated promotions that all abbreviate to AFC. K-1, GLORY, Enfusion
  and RISE award their own titles and stay promotion-scoped.
- **Test each authority fragment, not the whole string.** In `QP, WKN 60kg Titles` only
  the WKN half is a sanctioning body. Testing the whole string made `body:qp` -- a
  promoter's belt turned promotion-agnostic, merging unrelated promotions sharing an
  acronym. That mistake produced 136 bogus owners.

### Division: name beats weight

`weight_badge` is numeric pounds on 96% of title bouts and looks more canonical -- KOTC's
"Bantamweight" is 145 lb where everyone else's is 135. But the owner axis already scopes
the name, and keying on the number **fragments a belt across weigh-in variance**: +709
belts, worst cases catchweight. So: name first, weight only when the string names no
division.

Then two refinements:

- **Canonicalise the weight.** `121`, `121 lbs`, `65 kg`, `65kg` must not be four
  divisions. A bare number comes from `weight_badge`, which is always pounds.
- **Adopt the owner's own name for that weight.** `TFC Championship` at 135 lb joins
  `TFC Bantamweight Championship` rather than forming its own belt. 329 merges.

**When NOT to merge:** a weight-only division under a *distinctive* owner is usually a
real belt with no division. `UFC "BMF" Title Belt` shows up at 155 lb and 170 lb, and
`UFC Superfight Championship` at 265 lb. Merging those into lightweight / welterweight /
heavyweight destroys three genuine UFC belts. The owner axis already separates them; only
the weight axis splits them, and 136 owners (392 belts) sit in this ambiguous state --
either a named belt wrongly split by weight, or a promotion whose divisions genuinely have
no names. Indistinguishable without knowing the belt's own name, so both are left alone.

### "Undisputed" is a state, not a body

`Undisputed Heavyweight Title` names no sanctioning body. Keyed literally it becomes a
phantom `body:undisputed` that says nothing about who sanctions the belt, and it splits real
lineages -- Shields vs Hammer 2019 sat alone on it while the four belts it actually unified
sat elsewhere. All 95 such bouts are boxing and none names a body or Ring Magazine.

Expanded to the majors **that existed that year**, since the corpus runs 1977-2026:

```
>= 1988   WBC + WBA + IBF + WBO
1983-87   WBC + WBA + IBF        (WBO founded 1988)
<  1983   WBC + WBA              (IBF founded 1983)
```

Ring Magazine is deliberately **not** included: it is a merit title, not a sanctioning body,
and when it is at stake Tapology names it -- 115 strings do, and none of the 28 "Undisputed"
strings does. See `title-belt-overrides.md`.

## Rejection rules -- what is not a belt

| reject | bouts | why |
|---|---|---|
| `title_kind == "tournament"` | 12,560 | bracket rounds -- IBJJF finals, ADCC, Olympic draws |
| `catchweight` | 1,061 | see below |
| `Medal Match` / `Gold Medal` | 726 | amateur championship finals, ~1 per year/weight, no lineage |
| no division resolvable | 759 | cannot be placed |
| `Championships` + a year | 7 | event name in the title slot |

### Catchweight is not a division

A catchweight is a negotiated one-off. Of 408 distinct catchweight combos: **271 (66%)
are a single bout**, 125 repeat at *multiple* weights, and only 12 repeat at one weight.
`twc vacant championship` is 70 bouts across 23 weights from 105 lb to 210 lb; `ffp
vacant championship` 55 bouts across 21. These promotions label every bout "Championship"
at whatever weight the fighters made. Excluded -- flagged as title bouts with `belt = null`
rather than minting 408 phantom belts.

## Normalisation -- conservative, never fuzzy

Fuzzy matching is **actively harmful here**. Within one promotion, `81kg` and `81+kg` look
like a typo pair at 0.993 similarity and are genuinely different divisions (92 bouts use
`+` classes). Only exact, enumerated fixes:

- division vocabulary built from the data (53 tokens ending in `-weight`), with observed
  typos mapped: `straweight`->`strawweight` (191 bouts), `crusierweight` and
  `cruieserweight`->`cruiserweight`, `bantanweight`, `middlweight`, `fetherweight`,
  `superflyweight`
- `Muaythai` -> `Muay Thai`; remove the literal `*Numeric Weight*` placeholder (8 bouts)
- expand truncated divisions (`Welter Title` -> `Welterweight`) **only** before
  `Title|Championship|end`. Never touch a bare `Light`: 3,326 of 3,384 are the compound
  `Light Heavyweight` or `Light Flyweight`.
- **preserve `+`** in weight classes

## Unification bouts

965 bouts contest several belts at once (`EBU European & WBC Silver Super Lightweight
Titles`). The division suffix is shared, so: strip the shared axes, split the authority on
`,` / `&` / `and`, re-attach the division to each part. One bout produces N belt-links.
Leaving them unsplit invents composite belts -- `WBC World` alone drops from 133 phantom
belts to 86 once split.

**Tested and rejected:** stage 3b's `Title on Line` is **byte-identical** to the card's
`title_bout` in all 200 pilot bouts (37 multi-belt strings on each side), and
`Belt Status Before Fight` returns one value for the whole bout
(`Ring Magazine, WBA World & WBC World Heavyweight Titles` -> `Held by Liston`). Crawling
bout pages does **not** improve belt naming. Its value is belt *status* -- see
`belt-lineage.md`.

## Result

```
belt-bearing bouts 58,222  ->  belt-links 59,788  ->  DISTINCT BELTS 20,704
```

| sport | belt-links | belts |
|---|---|---|
| MMA | 34,619 | 11,196 |
| Boxing | 16,435 | 5,061 |
| Kickboxing | 4,460 | 2,208 |
| Muay Thai | 2,581 | 1,414 |
| Grappling | 705 | 385 |
| Boxing (Bare Knuckle) | 478 | 138 |
| all others | 510 | 302 |

**11,300 belts (55%) have exactly one bout** -- no lineage is reconstructible for them.
Lineage work is scoped to the **9,404 belts with two or more**, which is what
`logs/belt_worklist.txt` covers.

The stage-3b crawl is no longer scoped that way. A one-bout belt still has a bout page, and
that page carries weight, Pro/Am level, sport, belt status and the weigh-in rows -- none of
which need a lineage. Two gap crawls took HTML coverage of the ledger from 82.9% to
**99.9%**; see `stage3b-title-bouts.md`.

## Coverage, checked against Wikipedia

- **MMA 29/29.** Every promotion in *List of current MMA champions*, including Oktagon
  (78 title bouts) and UAE Warriors (59).
- **Boxing 36/64 organisations.** All four majors -- WBC 3,325 title bouts, WBA 2,503,
  WBO 2,384, IBF 1,588 -- plus IBO, EBU, NABF, WBF, OPBF and 27 more. The 28 absent are
  amateur/national federations (ABA, AIBA, IABA, PZB) that Tapology files under the
  continental confederations instead (World Boxing, EUBC, ASBC, AFBC, OCBC), whose bouts
  appear as `Gold Medal Match` strings the rules deliberately exclude, plus small or
  defunct bodies with no Tapology presence.
- **Kickboxing 26/29.** All ten sanctioning bodies, all the major promotions. Genuinely
  absent: TatNeft Cup. Present but near-empty: Prometheus (0 title bouts), IFMA (1),
  WKO (1), It's Showtime (1).

Validation: UFC `Flyweight Championship` -- 43 bouts, one string, **three belts**:
men's, women's, and the 2022 interim.

**Caution for any coverage check:** guessed slugs produce false gaps. Oktagon, FNG,
UAE Warriors and MMA GP all first appeared "missing" because their Tapology slugs are not
what you would guess (`fight-nights-russia-fnr`, not `fight-nights-global`). Search names
and event names as a fallback.
