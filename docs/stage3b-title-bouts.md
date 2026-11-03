# Stage 3b — title-fight bout pages

**Purpose:** capture the four fields that exist *only* on bout pages, for the ~7% of
bouts that are title fights.

Everything else on a bout page duplicates what the event page already gave us. This pass
exists for belt lineage, and secondarily for gym data.

---

## 1. What it adds

| field | why it matters |
|---|---|
| `Title on Line:` | the belt's formal name (`WBO World Middleweight Title`) |
| `Belt Status Before Fight:` | `Held by <name>` / **`Vacant`** — the lineage signal |
| `Gym` | fighter's team, with `(Primary)` / `(Other)` markers |
| `Fighting out of` | city, distinct from nationality |

Also present but already known: referee, disclosed pay, `Sport:`, bout billing, weight,
broadcast, event links, `Bout Links:`.

**Verified present in every sport tested** — boxing, MMA, karate, kickboxing, muay thai,
wrestling, sanda all return `Title on Line`; 7 of 8 also return `Belt Status Before
Fight` (slap fighting omits it). It is **not** an MMA-only field, so do not scope this
pass to MMA.

`Belt Status` has three states, not two: held, vacant, or absent. Sanda's
`MMASC Vacant Light Heavyweight Championship` returns `Vacant`.

---

## 2. Building the worklist

```python
import json
work = []
for line in open("data/bouts.jsonl"):
    b = json.loads(line)
    if b.get("title_kind") == "title" and b.get("bout_slug"):
        work.append(b["bout_slug"])
work = sorted(set(work))
```

**Filter on `title_kind == "title"` only.** Do not include `"tournament"`.

That distinction is the whole reason `title_kind` exists. Tapology writes
`Tournament Championship` in the same slot as a real title, so a naive
`"champion" in title_bout` match sweeps in every IBJJF bracket final, every ADCC round
and the 1960 Olympic boxing draw. Current split:

```
title       59,977    ← crawl these
tournament  12,511    ← skip: bracket rounds, not belts
other          556
none       747,996
```

Including `tournament` would add 12,511 pages (~6 h) of bracket rounds that carry no
belt information.

**Numbers** (recompute after the re-parse — `title_kind` gets rebuilt):

```
title bouts        59,977   (7.31% of 821,040)
without bout_slug       0   ← all are crawlable
distinct belts     20,874
by sport           MMA 34,897 · Boxing 15,100 · Kickboxing 4,580 · Muay Thai 2,807 · +10 more
estimated          59,977 pages at 37/min ≈ 27 hours
```

---

## 2b. The two gap crawls, and why the first worklist was short

The original worklist came from `logs/belt_worklist.txt`, which `belts.py` fills with bouts
on a belt of **>= 2 bouts** (`--min-bouts`). That was right for lineages -- a one-bout belt
has no lineage to reconstruct -- and wrong for everything else the page carries. Weight,
Pro/Am level, sport, belt status and the weigh-in rows do not need a lineage.

Two passes closed it:

```
 8,101  cancelled title bouts        210.6 min @ 37.9/min   0 anomalies, 0 parser errors
11,287  the remaining gap            294.6 min @ 38.3/min   0 anomalies, 13 dead slugs
```

The second pass split two ways:

| | | |
|---|---:|---|
| single-bout belts | 10,449 | never queued by the `>= 2` rule |
| multi-bout belts | 865 | belts that **crossed** the threshold after `belts.py` changed |

That second group is the one to remember. Adding `sport` to the belt key, undoing ONE's
2018 realignment, the gender fill and the comma-decimal fix all moved belts across the
`>= 2` line *after* the crawl had run, so their bouts were never in the worklist. **Re-run
`belts.py` and diff the worklist against `logs/bouts_done.txt` after any change to the belt
key**, or the gap reopens silently.

Result: HTML coverage of the title-fight ledger went **82.9% -> 99.9%**, 48 bouts short, of
which 13 are dead slugs.

### `--include-unindexed`

A cancelled bout is not on the card, so it is not in `bouts.jsonl`, so `build_worklist()`
cannot produce a row for it. The flag admits `--worklist` slugs that the index does not
know, taking `bout_id` from the leading number of the slug and sorting them last:

```bash
python3 -m pipeline.tapology_archive.crawl_stage3b     --worklist logs/cancelled_worklist.txt --include-unindexed --sleep 1.0
```

### Pacing

`--sleep` is a **floor**, not a fixed delay: a soft failure multiplies the pace by 1.15 up
to a 4 s ceiling, and it only decays back after 200 clean fetches. The original run
sustained 38.5 bouts/min, which implies ~1 s. `--sleep 1.0` reproduced that (38-40/min);
`--sleep 2.5` halves it to 18/min for no measured benefit. Do not go below 1.0 -- nothing
faster has been demonstrated, and the failure mode is a burned IP mid-run.

### Keeping the machine awake

A multi-hour crawl dies if the Mac sleeps, and this one is set to `sleep 1` on **AC as well
as battery** -- only the display being on was holding it up. Start caffeinate bound to the
crawler's PID so it releases itself on exit:

```bash
nohup python3 -m pipeline.tapology_archive.crawl_stage3b ... & CRAWL=$!
nohup caffeinate -i -m -s -w $CRAWL >/dev/null 2>&1 &
```

`-i -m -s` holds system, disk and idle sleep. Deliberately **not** `-d` -- the display
should still sleep; the crawl does not need pixels.

---

## 3. URLs

```
https://www.tapology.com/fightcenter/bouts/{bout_slug}
```

**Never construct a bout slug.** Two hand-built slugs during development returned HTTP
200 and silently redirected to `/fightcenter` — a valid-looking page that parses to
nothing. Always use the `bout_slug` captured from the event page. All 59,977 have one.

---

## 4. Implementation

**Copy `pipeline/tapology_archive/crawl_stage3.py`.** Everything that matters is already there and
proven over 104k pages: checkpointing, atomic writes, the archive, in-process browser
recovery, immediate-rotation-on-503, the LRU VPN pool with self-retiring regions, the
single-instance lock, the "Temporarily Unavailable" guards, and the completion flag.

Change exactly four things:

1. **Worklist** — bout slugs from §2 instead of event slugs from `events.jsonl`.
2. **URL** — `/fightcenter/bouts/` instead of `/fightcenter/events/`.
3. **Extractor** — `parse_bout_page()` instead of `parse_event()`. It already exists in
   `pipeline/parsers/tapology.py` and is tested against MMA, boxing, karate,
   kickboxing, muay thai, wrestling, sanda and a 2011 legacy page.
4. **Outputs** — write `data/bout_details.jsonl` keyed by `bout_slug`. Archive HTML to
   `data/html_bouts/<aa>/<bb>/<slug>.html.gz` (same sha1 sharding; **do not** mix it into
   `data/html/`, which is the event archive).

Keep the checkpoint in a **separate** file (`logs/bouts_done.txt`). Do not reuse
`logs/events_done.txt`.

### Bad-page guard

The event guard checks for `primaryDetailsContainer`. Bout pages don't have one — use
`boutComparisonTable` **or** the presence of any `Bout Information` label instead, or
`parse_bout_page()`'s own `parse_ok` flag. Getting this wrong means archiving error
pages, which is exactly how 450 events were silently lost during stage 3.

### QA

Reuse the anomaly pattern. Worth flagging per bout:
- `parse_ok` false (redirect / error page)
- no `Title on Line` on a bout we selected *because* it was a title fight
- `Belt Status Before Fight` absent (expected for some sports — count, don't alarm)
- fewer than 2 fighters

---

## 5. Pacing

Identical to stage 3 — this hits the same host under the same Cloudflare rules:

```bash
--sleep 1.0 --rotate-every 450
```

Do not go faster. Measured on 104k pages: the per-IP budget shrinks faster than the pace
gains (530 events/IP at 1.0 s vs 225 at 0.7 s, both ≈37/min). The VPN pool state in
`logs/vpn_pool.json` carries over, so the 11 already-retired regions stay retired.

---

## 6. After it finishes

Join back on `bout_slug`:

```
bout_details.bout_slug → bouts.bout_slug → bouts.event_slug → event_details
```

Then the belt-lineage query becomes possible: order title bouts for a given
`Title on Line` by event date and read `Belt Status Before Fight` to reconstruct who held
what, when, and which reigns began vacant.

Gym data lands per bout, so a fighter's gym history is `bout_details` filtered by their
slug and ordered by date — but only across their *title* fights, which is a biased
sample. If you want gym coverage for everyone, that needs a wider bout pass or fighter
pages, and neither is currently justified.

---

## 7. Optional narrowing

27 hours is the full-fidelity option. If that is too long:

- **MMA only** — 34,897 pages, ~16 h. Loses boxing lineage (15,100 bouts), which is the
  richest belt history in the dataset. Not recommended.
- **Modern only** (2000+) — cuts roughly a third, loses early lineage.
- **Sample by promotion** — take the top N promotions by title-bout count.

None of these are needed for correctness. The full pass is simply a day of unattended
crawling, and the archive means it never has to be repeated.
