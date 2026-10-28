# Tapology scrape — HANDOFF

Last updated: 2026-08-20. Nothing is running. All processes stopped, all data checkpointed.

Goal: build an **era-accurate, multi-sport** promotion-strength dataset to replace the
MMA-only footprint documented in `docs/promotion_strength.md`.

---

## 1. Current state

| Stage | Status | Output |
|---|---|---|
| 1 · Promotion index | ✅ **done** | 7,007 promotions |
| 2 · Event index | ✅ **done** | 87,605 events (index-level only) |
| 3 · Event details | ❌ **NOT STARTED — do this next** | — |
| 4 · Fighters | ❌ not started (IDs come from stage 3) | — |
| 5 · Daily increment | 🅿️ **parked, half-built** | see §7 |

**Important:** stages 1–2 give you *identifiers*, not fight data. There are **no bout
records, no fighters, and no per-sport counts yet**. Stage 3 is where the actual value is.

### Locations
- **Working pipeline (use this):** `~/ufc-elo-calculator/seeder_data/tapology_scrape/`
- **Snapshot copy (read-only convenience):** `~/Desktop/MMA data/`

---

## 2. The datasets

All JSONL, one object per line, verified 0 corrupt rows.

### `data/promotions.jsonl` — 7,007
```json
{"id": 61, "slug": "one-fighting-championship-ofc", "name": "ONE Championship"}
```

### `data/promotions_detail.jsonl` — 7,000
`promotion_id, name, headquarters, acronyms, also_known_as, logo_image_url,
promotion_links[{platform, url}]`
(7 promotions have no detail block on their page.)

### `data/events.jsonl` — 87,605
`event_id, event_slug, event_name, promotion_id, region_slug, region_name,
title_fight, title_name, raw_row_text`

`raw_row_text` is the **verbatim** row text, deliberately kept so nothing was discarded at
scrape time — it still holds date, venue, city/country, broadcaster, and (for upcoming
events) card previews with sport tags. Parse from it rather than re-crawling.

**Data caveat:** ~50 events near the end carry extra fields (`completed`, `event_date`,
`sport`, `last_seen`) and 8 of those have `promotion_id: null`. That's from a single test
run of the parked daily script (§7). Harmless and additive — stage 3 will fill the gaps.

### State files (`state/`)
- `promotions_done.txt` — the 7,007 completed promotion ids (stage-2 checkpoint)
- `crawl.log`, `monitor.log`, `status.json` — full history of the stage-2 run
- `chrome_profile/` — **189 MB of disposable junk** from the first run. Safe to delete:
  `rm -rf state/chrome_profile`

---

## 3. Cloudflare — the single most important thing to know

Tapology sits behind a **Cloudflare managed JS challenge** (`cf-mitigated: challenge`).

| Approach | Result |
|---|---|
| `requests` / `urllib` / `curl` | ❌ 403 |
| `curl_cffi` w/ Chrome TLS impersonation | ❌ 403 (needs JS execution, not just TLS) |
| Playwright **headless** (incl. `channel="chrome"` + stealth) | ❌ flagged, never clears |
| **Playwright headed real Chrome** (`channel="chrome", headless=False`) | ✅ **works** |

Harvesting `cf_clearance` to replay elsewhere is **not possible** — it's HttpOnly.

**Consequence:** the crawl needs a real GUI Chrome window and a logged-in desktop session.
It cannot run headless, on a server, or while the Mac is asleep.

### The configuration that finally worked
Ran ~5 hours straight, ~420 promotions/30 min, **zero blocks**:
- one VPN region, **held steady** — no proactive IP rotation
- `time.sleep(2.5)` between promotions, `await sleep(750)` between pages
- rotate IP **only** reactively, if Cloudflare actually blocks
- fresh `tempfile.mkdtemp()` Chrome profile per launch

---

## 4. Failure modes already hit (do not repeat)

1. **Corrupt JSONL → 8-hour crash-loop.** A flush killed mid-write left a half-written
   line; every restart then died *at file load*, before launching Chrome, and the
   supervisor spun 5,188 no-op restarts overnight.
   **Fixed:** `atomic_write()` (temp file + `os.replace`) and tolerant `load_jsonl()` that
   skips bad lines. Keep both in any new stage.

2. **Chrome profile lock collisions.** Reusing one `user_data_dir` across restarts made
   every relaunch die instantly (`TargetClosedError`).
   **Fixed:** disposable temp profile per launch.

3. **Hammering Cloudflare keeps a block warm.** Retrying every ~90 s during a block never
   cleared it. Only a genuinely **silent** window (Chrome fully closed, zero requests)
   lets it decay.

4. **A hard IP block lasted 8+ hours** after the crash-loop abused CF. Escalating silent
   waits up to 2 h did not clear it; **switching VPN region fixed it instantly**.

5. **Aggressive VPN rotation broke the user's network.** Rotating every ~150 promotions
   churned the connection (each switch drops *all* traffic ~15–35 s, including the Claude
   session). Reactive-only rotation is the safe setting.

6. **ExpressVPN split tunneling is unsupported on macOS 11+** (tested on 26.5.2). The CLI
   accepts `set splittunnel true` / `bypass:` rules and an isolated test even appeared to
   pass, but under sustained load it destabilised the VPN. **Leave it off.**
   CLI is at `/Applications/ExpressVPN.app/Contents/MacOS/expressvpnctl`
   (`connect <region>`, `status`) — GUI must be running or background mode enabled.

---

## 5. Parsing knowledge (hard-won selectors)

- **Promotion event rows:** `[id^="preview"]` matching `/^preview\d+$/`; the numeric id
  *is* the event id.
  ⚠️ Do **not** use `[id^="event-"]` — those wrappers are JS-injected and absent from a
  raw `fetch()` response.
- **Scope carefully.** A promotion page also links events from `#sidebar`,
  `#newsContentLiveEvents`, and `#mobileLiveParentContainer` — those belong to *other*
  promotions and will contaminate results. Only `#preview{id}` rows are the promotion's own.
- **Legacy records have no numeric id in the slug** (e.g. `/events/affliction-1-banned`,
  `/fighters/a-sol-kwan`). Always take the id from the container id where possible, and
  tolerate id-less slugs.
- **Pagination:** `?page=N`; a page yielding no *new* ids means the list is exhausted
  (pages repeat content past the end rather than 404ing).
- **Event page** (verified on the saved sample) yields in a single fetch:
  - per-sport bout counts — `"MMA Bouts: 10"`, `"Muay Thai Bouts: N"`, …
  - **14 bout links** (`/fightcenter/bouts/{id}-{slug}`)
  - **24 fighter links** (`/fightcenter/fighters/{id}-{slug}`)
  - production block: date/time, broadcast, promotion, ownership, venue, location,
    enclosure, announcers
- **Full field census** for every page type: **`FIELDS.md`** — that is the capture spec.
  The rule agreed with the user: **capture everything, judge features later.**

---

## 6. Stage 3 — event details (do this next)

**Input:** the 87,605 `event_slug`s in `data/events.jsonl`.
**Fetch:** `https://www.tapology.com/fightcenter/events/{event_slug}`

**Extract per `FIELDS.md`:**
- per-sport bout counts ← *this is what fixes the MMA-only limitation*
- Event Production block (date/time, broadcasts, promotion, ownership, venue, location,
  enclosure, announcers, event links, poster)
- full fight card per bout: order, bout id/slug, both fighters (id/slug/name/record-after),
  method + rounds + time, billing, weight, round format, pro/am, odds, age, weigh-in,
  height, reach

**Suggested outputs:** `data/event_details.jsonl`, `data/bouts.jsonl`,
`data/event_fighters.jsonl` (event↔fighter link table).

**Build it by copying `driver/crawl_stage2.py`** — it already has everything that matters:
checkpoint/resume, atomic writes, tolerant loading, in-process browser-crash recovery,
silent CF backoff, reactive VPN rotation. Swap the in-page extractor and the id list.

**Scale:** ~87.6 k pages ≈ **2 days** at the stable pace. Checkpointed, so it can run in
chunks across sessions.

**Open decision (never answered — ask the user):** crawl order.
Options discussed: everything oldest-first · prioritise majors/MMA/recent first ·
a bounded recent-era slice to validate the extractor first · or strength-model inputs only
(per-sport counts, skipping fight cards — much faster but yields no bouts/fighters).

---

## 7. Stage 4 · fighters, and Stage 5 · daily increment

**Stage 4** — fighter pages for never-seen fighters. The fighter IDs come out of stage 3,
so the population size is unknown until then. Don't guess it in advance.

**Stage 5 — the daily job.** Parked at `driver/_parked/crawl_daily.WIP.py`.
The *discovery* half is built and tested: it reads
`https://www.tapology.com/fightcenter?schedule=results` (**completed** events only,
newest-first, ~25 rows/page), upserts new ones, refreshes ones previously seen as
upcoming, parses date + sport, and resolves each new event's promotion by opening the
event page. A real run found **8 new events and refreshed 50**.

It is parked because the daily increment must mirror the whole pipeline:

```
new completed events → their event details → their bouts → never-seen fighters
```

Stages 3–4 must exist first; then call their extractors from this script and schedule it.
**Scheduling note:** it needs headed Chrome, so it must be a local **launchd LaunchAgent**
in the user's GUI session — not cron on a server, and not a cloud job. Nothing is
scheduled right now.

---

## 8. Running things

```bash
cd ~/ufc-elo-calculator/seeder_data/tapology_scrape

python3 driver/status.py            # progress snapshot
tail -f state/crawl.log             # live detail log
```

Resume a long crawl (supervisor auto-restarts the crawler on browser crashes and stops
when complete):
```bash
caffeinate -dimsu &                                          # keep the Mac awake
nohup bash driver/supervise.sh > state/supervisor.stdout 2>&1 &
```

Stop everything gracefully (the crawler flushes its checkpoint on SIGTERM):
```bash
pkill -f supervise.sh; pkill -TERM -f crawl_stage2.py
pkill -f driver/poll.py; pkill -f 'caffeinate -dimsu'
```

`driver/poll.py` writes a heartbeat to `state/monitor.log` every 30 min and flags stalls.

### Files
```
driver/crawl_stage2.py    the stage-2 crawler — TEMPLATE for stage 3
driver/supervise.sh       restart supervisor w/ crash-loop backoff
driver/poll.py            30-min heartbeat writer
driver/status.py          progress snapshot
driver/next_batch.py      batch selector (from the older interactive approach)
driver/_parked/           the WIP daily script
ingest/                   ingesters for browser-tool output (older approach)
reference/                standalone in-page extractor snippets
FIELDS.md                 full field census — the capture spec
README.md                 method + guardrails
```

---

## 9. Gotchas for whoever picks this up

- **The Mac must stay awake and logged in.** Headed Chrome can't run through sleep;
  use `caffeinate -dimsu`.
- **Keep the VPN on one region.** Only rotate reactively. Don't enable split tunneling.
- **Never let a crawler exit-and-restart in a tight loop** — that's what earned the
  multi-hour IP block. Recover in-process instead.
- **`raw_row_text` is deliberate.** Don't "clean it up" — it's the audit trail for fields
  that weren't parsed into columns yet.
- **Sport cannot be inferred from promotion names** (~56 % unclassifiable). It must come
  from event pages — the whole point of stage 3.
- Known under-representation the project exists to fix: **Karate Combat ~1 % MMA**
  (erased entirely by a `sport=='mma'` filter), **ONE ~18 %**, **RIZIN ~84 %**.
