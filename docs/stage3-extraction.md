# Stage-3 extraction: what is gettable, and how the page varies

Verified against 7 event pages and 3 bout pages spanning 2009–2026, five sports and
four promotion tiers. Extractor: `pipeline/parsers/tapology.py`
(`parse_event`, `parse_bout_page`). Raw HTML kept in `samples/` and `samples/live/`.

## Verdict

Every field requested is present and extracted. Nothing needed a second request beyond
the event page, except the four fields that only exist on the bout page (below).

**Event page** — promotion (+id, +logo), full title, date, location (+region), poster,
the whole Event Production table, per-sport bout counts, promotion links, event links,
full fight card (order, billing, weight, format, sport, title, method, duration, both
fighters with id/result/record-after/nationality/headshot), the per-bout comparison
table, cancelled & fizzled bouts, weigh-in results, awards & bonuses, fight referees.

**Bout page adds** (and only these): `Pro Record At Fight`, `Fighting out of`, `Gym`,
`Referee`, disclosed pay, `Title on Line`, `Belt Status Before Fight`, `Bout Links`,
and — on non-MMA bouts only — an explicit `Sport:` label.

## The one structural fact that drives the parser

Tapology ships desktop **and** mobile copies of most values in the same HTML. The mobile
copy always carries the class `md:hidden`. Deleting those elements once, before parsing,
removes every duplicate site-wide. Without it you get `"Philippines Philippines"`,
`"170.0 lbs (77.1 kgs) 170.0 lbs (77.1 kgs)"`, doubled production rows and 24 bout
blocks where there are 12.

## How the tables vary, and the rule that absorbs it

| Variation seen | Where | Rule used |
|---|---|---|
| Comparison table has 6–11 rows depending on sport/era/status | every bout | Never index rows. Key each row by its **middle label cell**, take the nearest non-empty cell left and right. Unknown labels land under their own canonical key instead of being dropped. |
| Upcoming bouts swap `Weigh-In Result` → `Latest Weight`; add `Title`, drop `Record After Fight` | UFC BJJ 10 | Same rule — the new label just appears as a new key. |
| Row cell count changes (5 vs 7 `<td>`) | age/weight rows | The extra cells are the `md:hidden` mobile twins; gone after the strip. |
| `Pro Record At Fight` + record-after packed in one cell on old layouts | ONE FC 1 | Both a combined cell and a separate `Record After Fight` row are captured. |
| Missing values written as `N/A`, `-`, empty | reach, gym, referee | `val()` normalises all of them to `None`. |
| Header strip colour changes per sport (`text-tap_darkgold` MMA, other palettes for karate/kickboxing/grappling) | every bout | **Do not select on colour.** Take the first child `div` of the bout block; its spans are `[title, method, duration]` by position. A colour selector silently drops every non-MMA bout. |
| Weight pill colour likewise (`bg-tap_darkgold` / `bg-red-700` / `bg-sky-700`) | every bout | Match `bg-*` by regex, keep the class as a sport signal. |
| Round format alone (`5 x 5`) or sharing a line (`3 x 3 \| Pro`) | karate | Regex-search the leaf element, capture the pro/am flag alongside. |
| No `<Sport> Bouts: N` label at all | Karate Combat | Fall back to counting the card by each bout's own sport banner; `bout_counts_source` records which path was used. |
| No bout numbers at all (bracket events) | IBJJF | `order` stays `None`; `card_position` always filled from page order. |
| No method/duration (tournament results) | IBJJF | `method`/`duration` `None`, W/L still captured. |
| Disclosed-pay labels contain the fighter's name (`Makhachev Total Disclosed Pay:`) | bout page | Matched by regex suffix, stored in `disclosed_pay` keyed by name. |
| Fighter slugs take any form (`40148-islam-makhachev`, `134267`, `a-sol-kwan`) | everywhere | **A fighter has no id -- the slug is the identity.** Whatever follows `/fighters/` is captured verbatim; leading digits are incidental, not a key. Numeric ids elsewhere (`event_id`, `bout_id`) come from real DOM container ids (`preview{n}`, `boutFullsize{n}`), not from slugs. |
| Slug does not follow from the displayed name | `a-sol-kwan` for "A Sol Kwon" | Never reconstruct a slug from a name, or a name from a slug. |
| `Fighter Comparison` heading is mobile-only | bout page | Anchor on `#boutComparisonTable`, not the heading. |
| Method vocabulary is sport-specific (`Pinfall`, `Tech Fall` in wrestling) | RAF 10 | Classify header spans by their **class signature** (`uppercase`=method, `leading-relaxed`=duration), never by method keywords or span count. A keyword whitelist files "N/A, Pinfall" as the bout title. |
| Duration line absent entirely | wrestling cards | Slot-by-class handles it; `duration` stays `None` while `method` still lands. |
| Finish vs distance in one field | all sports | `3:34 Round 2 of 5, 8:34 Total` -> `finish=True` + round/time/scheduled; `3 Rounds, 15:00 Total` -> `finish=False`. |
| Venue with no region link | UFC Doha | `region_slug` legitimately `None`; `location_header` falls back to the production block. |
| Wrestling/grappling bouts carry **no** `Pro Record At Fight`, `Record After Fight`, `Betting Odds` or `Weigh-In Result` | RAF 10 | Not a parse failure -- Tapology keeps records sport-segregated, so `record_after` is genuinely `None` and a wrestling result never touches the MMA record. |
| Status vocabulary is open (`Completed`, `In Progress`, …) | header | Taken positionally, not from a whitelist. |

## Operational notes

- **Bout URLs must be read off the event page, never constructed.** Two hand-built bout
  slugs both resolved to `/fightcenter` — Tapology redirects bad slugs instead of 404ing.
  `parse_ok` flags that case (no production block and no bouts / no info and no table).
- Recent events omit the year in promotion-page row text ("Saturday, August 15, 5:30 PM
  ET"). The year comes from the event page (`August 15, 2026`).
- Promotion name differs between the header link (`UFC`, `ONE`) and the production block
  (`Ultimate Fighting Championship`, `ONE Championship`). Both are captured.

---

# Cross-sport / cross-era survey (48 events, 1987–2026)

Raw HTML in `samples/sports/`, index in `samples/sports/index.json`. Three events per
sport (very old / old / recent) wherever three existed.

## Tapology emits 24 bout sports

Counted over all 821,046 bouts, not sampled. `sport_base` folds `level` (Kids/Juniors)
and `modified_rules` back into the parent sport; the raw `sport` field has 87 values
because it also carries the `Professional`/`Amateur` prefix and those variants.

```
517778  MMA                  1038  Boxing (Raw)          239  MMA (Bare Knuckle)
178640  Boxing                981  Karate                178  Vale Tudo
 61398  Kickboxing            631  Slap Fighting         169  Combat Jiu-Jitsu
 29760  Muay Thai             382  Sanda                 139  Kun Khmer
 17661  Grappling             364  Pancrase               52  Judo
  5438  Boxing (Bare Knuckle) 346  Lethwei                41  Sambo
  2449  Boxing (Cage)         306  Wrestling              37  Ice Fighting
  1609  Custom Rules                                      14  Taekwondo
  1396  Shootboxing
```

**Corrects the 48-event survey below, which reported 11.** That survey concluded Sambo,
judo, sanda, vale tudo, pancrase and ice fighting "all render as MMA". They do not --
each is its own banner value. The survey missed them because they are rare: the six
together are 1,054 bouts, 0.13% of the corpus, so 48 events could not have contained
them. Treat the sport list as a full-corpus count only; a sample will always undercount
the tail.

## `bout_counts` must be counted, never trusted

The production block only counts sports Tapology maintains counters for (MMA, Grappling,
Boxing, Muay Thai). On a mixed card it under-reports: Karate Combat 62 declares
`Grappling Bouts: 2` for a 12-bout card that is 10 karate + 2 grappling. The card is
authoritative; `bout_counts_declared` keeps the page's own numbers and
`bout_counts_agree` flags disagreement.

## `title_bout` is an annotation, not a title

Grappling cards put the bracket stage in the same slot ("Openweight Finals"). Across 404
bouts: 28 real titles, 45 tournament stages. `title_kind` ∈ `title | tournament | other`
separates them — without it, "fetch bout pages for title fights" would cover 18.1% of
bouts instead of 6.9%.

## What exists per sport

| | records | odds | weigh-ins | referees | methods |
|---|---|---|---|---|---|
| MMA (incl. sambo/sanda/vale tudo/pancrase/judo) | ✅ | ✅ (2019+) | ✅ | ✅ | ✅ |
| Boxing / cage boxing | ❌ | ❌ | ✅ | ✅ | ✅ |
| Bare knuckle | ❌ | ❌ | ✅ | ❌ | ✅ |
| Wrestling | ❌ | ❌ | ❌ | partial | ✅ |
| Karate, slap, lethwei, shootboxing, combat JJ | ❌ | ❌ | ❌ | partial | ✅ |
| Grappling | ❌ | ❌ | ❌ | ❌ | 2023+ only |

Only MMA-classified bouts carry `record_after`. Betting odds appear on MMA only, and only
from ~2019. Grappling methods appear only on recent events (none in 1994 or 2012).

## Further variations found

- Header strip is **absent entirely** on bouts with no recorded result — the fighter row
  becomes the first child. Guard by locating the row that carries fighter links.
- Status and sport label can arrive **merged** in one header string
  ("Completed Muay Thai Event").
- Events can have **zero bouts** and still be valid pages (WCK Muay Thai 2001).
- New comparison labels: `Fight Day Weight`, `Weight Gain` (bare knuckle hydration).
- New production labels: `Co-Promoter`, `Matchmaker`, `PPV Buys / Buyrate`,
  `Tapology Accounts`, `Interviews`, `Preliminary Card`, `Name`, `Also Known As`.
- Lethwei carries free text in the expanded block ("Rule Modifications: Automatic draw if
  no knockout") — captured as `bout_notes`.
