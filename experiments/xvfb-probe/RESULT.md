# Can the Tapology crawler run in a container?

**Question.** `HANDOFF.stage2.md` §3 concluded that Cloudflare requires *headed real Chrome* on a
logged-in macOS desktop, which would mean the crawler can never be containerised or run as a cloud
job. That table has an untested cell: **headed bundled Chromium**. Only *headless* Chromium and
*headed real Chrome* were ever measured.

**Date.** 2026-10-24. Cloudflare configuration is a moving target; re-run before relying on this.

## Setup

`python:3.12-slim` + Xvfb 1920x1080x24 + Playwright bundled Chromium, `headless=False`,
`--no-sandbox --disable-dev-shm-usage`, `DISPLAY=:99`. Built `--platform linux/arm64` (native on
Apple Silicon). Google Chrome was **not** available — `playwright install chrome` has no arm64
build, which is itself useful to know.

Total requests to Tapology across both probes: ~10, paced at 1.0 s.

## Result — it clears

```
seed nav: HTTP 200   cf-mitigated=None   title='Arctic Combat 12 | Event | Tapology'
seed verdict: CLEARED   (282,422 bytes of real markup)

same-origin fetch() through the cleared page, 1.0s pacing:
  HTTP 200  264,492b  CLEARED   126980-samake-vs-knapp
  HTTP 200  690,989b  CLEARED   142341-ufc-330
  HTTP 200  476,561b  CLEARED   73893-bfc-64
```

The documented working primitive — clear once in a real browser, then fetch every subsequent page
with same-origin `fetch()` inside that page — works unchanged inside the container.

Diagnostics from the cleared page:

| | |
|---|---|
| `navigator.webdriver` | **`true`** — the automation flag was visible and it cleared anyway |
| `navigator.userAgent` | `Mozilla/5.0 (X11; Linux x86_64) … Chrome/156.0.0.0` |
| `navigator.platform` | `Linux x86_64` (Chromium reports x86_64 even on aarch64) |
| `cf-mitigated` response header | absent — no challenge was issued at all |

## Two corrections to the original conclusion

1. **Real Google Chrome is not required.** Bundled Chromium clears it, provided it is *headed*.
   So headed-ness is the load-bearing property, not the Chrome branding.
2. **macOS is not required.** Linux + Xvfb clears it, so a GUI desktop session is not the
   constraint either.

The original finding that **headless** never clears is not contradicted — headless and
headed-under-Xvfb present differently, and only the former was ever tested.

`navigator.webdriver: true` passing suggests Tapology's Cloudflare policy is not aggressive on
automation signals, which is consistent with the docs' own measurement that the limit is **soft**
(transient 503s under load) rather than a hard bot ban.

## What is still untested

| variable | status | how to close it |
|---|---|---|
| **Datacenter IP reputation** | **Azure: cleared on 2026-10-26** (see the dated section below). AWS is still untested | This ran from a residential IP. Cloudflare scores AWS/GCP ranges far more harshly. Cheapest test: a GitHub Actions `ubuntu-latest` job (free, Azure datacenter IPs), or a t3.micro once the AWS account exists in Phase 1 |
| Sustained volume | open | 10 requests here; the per-IP budget findings in the docs came from 104k pages |
| amd64 + real Chrome | not run | emulated-only on this host, and moot if Chromium works |

## Consequence

If the datacenter-IP test also passes, the crawler is containerisable and the DAG can run fully in
the cloud — no local edge agent, no launchd, no GUI session. If it fails, the blocker is **IP
reputation, not the browser**, and the fix is a residential/ISP proxy or a browser-as-a-service
provider rather than a different browser setup.

Either way the design is now evidence-based. See `DESIGN.md` §4.4.

## Re-running

```bash
docker build --platform linux/arm64 -t xvfb-probe .
docker run --rm xvfb-probe            # probe.py  — both channels, navigation + one fetch
```

---

## 2026-10-26 — datacenter-IP test (GitHub Actions, Azure)

**Question.** The 2026-10-24 run cleared from a residential IP. Does the same harness clear from a
cloud/datacenter IP? That is the one variable `DESIGN.md` §4.4 left open.

**Setup.** Harness files unchanged (`Dockerfile`, `probe.py`, `probe2.py`), built and run on a
GitHub-hosted `ubuntu-latest` runner. Workflow: `github-actions-probe.yml` in this folder (it ran
from the throwaway branch `probe/datacenter-ip`, which has since been deleted; run
[40348186840](https://github.com/yousefehammoudeh/fightgrade/actions/runs/40348186840)).

| | |
|---|---|
| egress IP | `20.171.2.198`, Phoenix AZ, **AS8075 Microsoft Corporation** (Azure `westus3` runner) |
| arch | `x86_64` (the residential run was arm64), so `playwright install chrome` succeeded and **real Google Chrome was probed too** |
| Playwright | 1.64.1, Chromium and Chrome both report `Chrome/156.0.0.0` |
| total Tapology page requests | 8: probe.py 2 × (nav + 1 fetch), probe2.py 1 nav + 3 fetches, 1.0 s pacing, no retries. probe2 was gated on probe.py clearing |

### Result: it clears from a datacenter IP

`probe.py`, both channels headed under Xvfb:

```
channel            http  cf-mitigated  title                                 bytes    nav       fetch (status, bytes)
chrome             200   None          'Arctic Combat 12 | Event | Tapology' 256,123  CLEARED   200, 240,657b
bundled-chromium   200   None          'Arctic Combat 12 | Event | Tapology' 254,202  CLEARED   200, 242,008b
navigator.webdriver = true (both)   platform = Linux x86_64   hardwareConcurrency = 2
```

`probe2.py`, clear once and then same-origin `fetch()` (the crawler's real access pattern):

```
seed nav: HTTP 200  cf-mitigated=None
seed verdict: CLEARED   title='Arctic Combat 12 | Event | Tapology'

same-origin fetch() through the cleared page, 1.0s pacing:
  HTTP 200  240,383b  CLEARED      126980-samake-vs-knapp
  HTTP 200  667,178b  CLEARED      142341-ufc-330
  HTTP 200  452,751b  CLEARED      73893-bfc-64
```

**Reading the evidence.**
- No `cf-mitigated` header on any navigation, so Cloudflare issued **no challenge at all** to an
  Azure IP, the same as it did for the residential one.
- `probe.py` prints `fetch=TINY/ERROR` for both channels. That comes from the harness, not from
  Tapology: `probe.py` classifies only the first 1,200 characters of the fetch body (`head`), and
  anything under 3,000 characters with no title falls through to `TINY/ERROR`. The real response
  was HTTP 200 with about 240 KB, the same size as `probe2.py`'s full-body `CLEARED` fetch of the
  same URL (240,383b). `probe2.py` classifies the whole body, so treat it as the authoritative
  fetch check.
- Pages are about 5–10% smaller than in the residential run (seed 254–256 KB vs 282 KB). Every
  page carried the event markup, so this is ad or geo content, not a degraded page.
- Real Chrome and bundled Chromium behave identically, which supports the 10-24 finding that
  headed-ness matters and Chrome branding doesn't.

### Verdict

From an Azure datacenter IP, the containerised harness (headed Chromium under Xvfb) cleared
Cloudflare at once, the same as from a residential IP. There was no challenge, the seed returned a
real event page, and all same-origin fetches came back as full event pages, even with
`navigator.webdriver: true`. **IP reputation is not the blocker, at least for Azure ranges at this
volume, so the crawler is containerisable from cloud IPs.** Three things are still not measured:
(1) **AWS** egress, which is what Dagster+ Serverless uses. Cloudflare scores by ASN, and AS8075 is
not AS16509. (2) **Sustained volume** from one datacenter IP, which was 8 requests here, while the
per-IP budget findings came from 104k residential pages. (3) **Shared egress**: Serverless IPs are
shared with other tenants, so their reputation is outside our control.

### What this means for where Dagster runs the crawler

- **Default: Dagster+ Serverless** (or any cloud container) is now a viable target. It needs a
  container image with Xvfb and headed Chromium, which is exactly this `Dockerfile`. A Hybrid agent
  on the Mac is no longer *required* just to get past Cloudflare.
- **Before committing to it,** run this same probe once from the real Serverless runtime, which is
  AWS. Run it as a one-off Dagster op that executes `probe2.py`, keep it at about 10 requests, and
  note the egress IP. If AWS clears, ship Serverless. If AWS is challenged, the fallback stays
  cheap: a Hybrid agent on the Mac, or a GCE or Azure container as the crawler's compute.
- **Volume is the next risk, not the browser:** keep 1.0 s pacing, a single crawler, and
  content-based "Temporarily Unavailable" detection. A nightly increment is small, and a full
  re-crawl would be the real test of a datacenter IP's budget.
