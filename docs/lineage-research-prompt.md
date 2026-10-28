# ChatGPT Pro research prompt — belt lineages for verification

Paste everything below the line. It deliberately does **not** contain our own answers, so the
result is an independent reference we can diff against rather than a confirmation of what we
already believe.

---

You are compiling a reference table of championship lineages in combat sports. I will verify a
database against it, so **accuracy and explicit uncertainty matter far more than coverage**. Take
your time. Work through the list in order. Do not summarise or truncate.

## What I need

For each organisation and division listed below, the **complete list of title reigns** in
chronological order, from the first champion to the current one.

## Output format — one line per reign, pipe-delimited, no prose between rows

```
ORG | DIVISION | GENDER | TYPE | CHAMPION | WON | HOW | LOST | CONFIDENCE | SOURCE
```

| field | values |
|---|---|
| `ORG` | exactly as I list it below |
| `DIVISION` | the division name **as that organisation used it at the time** |
| `GENDER` | `M` or `W` |
| `TYPE` | `undisputed` or `interim` |
| `CHAMPION` | full name, as commonly written |
| `WON` | `YYYY-MM-DD`, or `UNKNOWN` |
| `HOW` | one of: `beat_champion`, `vacant_title`, `tournament_final`, `promoted_from_interim`, `inherited_<ORG>` (title carried over when this organisation absorbed another), `awarded` |
| `LOST` | `YYYY-MM-DD`, or `current`, or `vacated_YYYY-MM-DD`, or `stripped_YYYY-MM-DD` |
| `CONFIDENCE` | `high` (multiple independent sources agree), `medium` (one solid source), `low` (contested or thin) |
| `SOURCE` | shortest useful citation, e.g. `Wikipedia: List of UFC champions`, or the official site |

## Rules that matter — these are where references usually go wrong

1. **Interim titles are separate rows** with `TYPE=interim`. An interim win does **not** end the
   undisputed champion's reign. When an interim champion is later elevated, that is a **new
   undisputed row** with `HOW=promoted_from_interim`.
2. **Divisions get renamed.** Give the name used at the time, and add a `# note:` line when an
   organisation renamed or redefined a division (weight limit changed, name reused for a different
   weight). This is the single most common source of error.
3. **Titles carried in from an absorbed organisation** get `HOW=inherited_<ORG>` naming the
   organisation the champion came from.
4. **Tournament finals that founded a lineage** get `HOW=tournament_final`. Many organisations
   crowned their first champion in a tournament rather than a title bout.
5. **Vacancies and strippings** must appear in `LOST`. Do not silently bridge a gap.
6. **Where sources disagree on a date, use the more widely cited one, set `CONFIDENCE=low`, and add
   a `# conflict:` line** giving both dates and who says what. Do not average or guess.
7. If a division has **no** documented lineage, write one line: `ORG | DIVISION | - | - | NO
   DOCUMENTED LINEAGE | - | - | - | - | -`. Do not invent.
8. Dates are the **event date in the event's local timezone**. Flag any you are unsure about.

## Targets

Work through these in order. The parenthesised note is what I most expect to be difficult.

### MMA
1. **UFC** — all divisions, men's and women's, including interim (division renames in 2001; WEC and
   Strikeforce titles absorbed)
2. **ONE Championship** — MMA divisions only, men's and women's
3. **Bellator** — all divisions (early divisions founded by season tournaments)
4. **PFL** — all divisions (season/tournament format, distinguish season winners from champions)
5. **RIZIN** — all divisions
6. **KSW** — all divisions
7. **Cage Warriors** — all divisions
8. **LFA** (Legacy Fighting Alliance, and its predecessors Legacy FC and RFA)
9. **Invicta FC** — all divisions, women's
10. **ACA** (Absolute Championship Akhmat, formerly ACB)
11. **King of the Cage** — all divisions (note: KOTC calls its 145 lb division "Bantamweight")

### Boxing — sanctioning bodies, not promoters
12. **WBC** — World titles, all 17 divisions, men's and women's
13. **WBA** — World titles (distinguish Super/Regular where the WBA recognises both)
14. **IBF** — World titles
15. **WBO** — World titles
16. **IBO** — World titles
17. **The Ring** — magazine titles
18. **EBU** (European), **British (BBBofC)**, **Commonwealth** — these three, main divisions only

### Kickboxing
19. **GLORY** — all divisions, men's and women's
20. **K-1** — all divisions (distinguish the original K-1 from K-1 World GP and the current K-1 Japan)
21. **Enfusion**, 22. **RISE**, 23. **Shoot Boxing**, 24. **ONE Championship** (kickboxing titles)

### Muay Thai
25. **Lumpinee Stadium**, 26. **Rajadamnern Stadium** — these two are the historic benchmarks
27. **WBC Muay Thai**, 28. **WMC** (World Muaythai Council), 29. **ONE Championship** (Muay Thai titles)

### Grappling / submission
30. **ADCC** — all weight classes plus absolute, men's and women's (biennial, so "reign" means
    reigning champion between editions)
31. **IBJJF** — black belt World Championship gold medallists by weight, if a lineage is meaningful
32. **Polaris**, 33. **UFC BJJ**

### Other sports
34. **BKFC** (Bare Knuckle FC) — all divisions
35. **BKB** (BKB / Bare Knuckle Boxing UK)
36. **Power Slap** — all divisions
37. **Karate Combat** — all divisions
38. **World Lethwei Championship**
39. **Real American Freestyle** (wrestling)
40. **Shoot Boxing** — S-cup and title lineages

## Finally

End with a section headed `## GAPS AND CAVEATS` listing:
- organisations or divisions where you could not find a reliable lineage, and why
- any lineage where the sources materially disagree
- any division you believe was renamed or redefined, with the date and both names

Do not pad. A short honest table beats a long confident one.
