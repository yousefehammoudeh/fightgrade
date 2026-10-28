"""Confirmation: clear once, then fetch several pages same-origin, as the DAG would."""
import json, time
from playwright.sync_api import sync_playwright

SEED = "https://www.tapology.com/fightcenter/events/108597-arctic-combat-12"
PAGES = [
    "https://www.tapology.com/fightcenter/events/126980-samake-vs-knapp",
    "https://www.tapology.com/fightcenter/events/142341-ufc-330",
    "https://www.tapology.com/fightcenter/events/73893-bfc-64",
]
CHALLENGE = ("just a moment", "challenge-platform", "cf-mitigated", "cf_chl_opt")

def verdict(body):
    low = body.lower()
    if any(m in low for m in CHALLENGE):          return "CHALLENGE"
    if "temporarily unavailable" in low:          return "TEMP_UNAVAILABLE"
    if "primarydetailscontainer" in low:          return "CLEARED"
    if len(body) < 3000:                          return "TINY"
    return f"UNKNOWN({len(body)}b)"

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    r = pg.goto(SEED, wait_until="domcontentloaded", timeout=60000)
    print(f"seed nav: HTTP {r.status}  cf-mitigated={r.headers.get('cf-mitigated')}", flush=True)
    time.sleep(10)
    seed_body = pg.content()
    print(f"seed verdict: {verdict(seed_body)}   title={pg.title()!r}", flush=True)

    print("\nsame-origin fetch() through the cleared page, 1.0s pacing:", flush=True)
    for u in PAGES:
        res = pg.evaluate("""async (u) => {
            const r = await fetch(u, {credentials:'include'});
            const t = await r.text();
            return {status: r.status, body: t};
        }""", u)
        print(f"  HTTP {res['status']}  {len(res['body']):>7,}b  {verdict(res['body']):<12} {u.rsplit('/',1)[-1][:40]}", flush=True)
        time.sleep(1.0)
    b.close()
