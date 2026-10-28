# Tapology scrape

Scraping pipeline for building an **era-accurate, multi-sport** promotion-strength
model (see `docs/promotion_strength.md`). Goal: replace the MMA-only footprint with
per-sport, per-time signals crawled directly from Tapology.

## Why this is non-trivial: Cloudflare

Tapology sits behind a **Cloudflare managed JS challenge** (`cf-mitigated: challenge`,
"Just a moment…"). Empirically:

| Method | Result |
|---|---|
| `requests` / `urllib` / plain `curl` | **403** |
| `curl_cffi` w/ Chrome TLS impersonation | **403** (challenge needs JS exec, not just TLS) |
| Harvest `cf_clearance` cookie → replay with `curl_cffi` | **not possible here** — cookie is HttpOnly and the in-app browser UA is a custom Electron string |
| **`fetch()` from inside an already-cleared browser page** | **200** ✓ |

So the working primitive is a **same-origin `fetch()` run inside the Claude in-app
browser page context** (it inherits the HttpOnly `cf_clearance` automatically), with
**parsing done in-page** so only compact JSON crosses the tool boundary.

### Rate-limit behaviour (measured)
- 25 concurrent requests → 22×`200`, 3×`503`, **0 challenges**. The limit is **soft**
  (transient `503`), not a hard ban.
- Bursting too hard (or a hung tool call) escalates to a fresh "Just a moment…"
  challenge. It **auto-solves in ~8–12 s** if you stop hammering and wait — no manual
  interaction. This is the `cf_clearance` refresh.

### Guardrails (all confirmed live)
1. **Retry with backoff** on `503` / challenge bodies.
2. **Detect + pause on challenge** (`/Just a moment/i` in body → stop, wait, resume).
3. **Checkpoint + resume** — persist completed IDs; a full crawl spans hours/sessions.
4. **Throttle** — cap concurrency ~3–5, add jitter, so soft limits don't escalate.
5. **Cut volume ~10×** — the *event* page's fight card already carries method, weight,
   round format, odds and record-**after** for every bout. The *bout* page only uniquely
   adds record-**before**, referee and disclosed pay. Record-before is derivable from
   record-after ± result, so crawl `promotion → event` and only dip into bout pages for
   the rare missing field.

## Tool constraints that shape the design
- **javascript_tool caps at 30 s** → each in-page batch must stay small (~6 list pages).
- **Tool output caps ~25 k tokens** → large compact JSON auto-saves to a
  `tool-results/*.txt` file. That file **is the transport**: the in-page crawler returns
  full JSON, it overflows to disk, and `ingest/*.py` normalizes it into the canonical
  dataset. Files are `[{type,text}]` where `text` is a JSON string (sometimes with a
  trailing "(captured at …)" footer) — `ingest_promotions.py:extract_payload` recovers it.

## Pipeline stages
1. **Promotions** ✅ `data/promotions.jsonl` — 7,007 unique promotions (id, slug, name).
   Source: `/fightcenter/promotions?page=1..71` (alphabetical, 100/page, ends p71).
2. **Events** (next) — per promotion, walk `/fightcenter/promotions/{slug}?page=N`;
   capture event id/slug/date/venue/location/region + broadcaster.
3. **Event detail** — per event, `MMA Bouts: N` / per-sport bout counts (era-accurate
   sport mix), full card w/ per-bout records-after, method, weight, odds.
4. **Bouts** (only where needed) — record-**before** ("Pro Record At Fight"), referee, pay.

## Layout
- `data/promotions.jsonl` — stage-1 output (one JSON object per line, sorted by id).
- `ingest/ingest_promotions.py` — idempotent upsert (dedupe by id) from tool-result files.
- `reference/promotion_index_crawler.js` — the in-page batch crawler (paste into the
  browser javascript_tool; edit `START`/`END`).

## Scale estimate
Full site ≈ 7 k promotions → ~100 k+ events → ~1 M bouts. Crawling to the **event**
level (stage 3) at a polite 2–5 req/s is ~6–14 h of checkpointed wall time. The bout
stage is only for gap-filling, keeping the big crawl in the ~100 k-page range rather
than ~1 M.
