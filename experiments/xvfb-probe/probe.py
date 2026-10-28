"""Does headed Chrome/Chromium under Xvfb clear Tapology's Cloudflare challenge?

Deliberately tiny: at most 2 page loads + 1 same-origin fetch per channel.
"""
import json, platform, sys, time
from playwright.sync_api import sync_playwright

URL1 = "https://www.tapology.com/fightcenter/events/108597-arctic-combat-12"
URL2 = "https://www.tapology.com/fightcenter/events/126980-samake-vs-knapp"

CHALLENGE_MARKERS = (
    "just a moment", "challenge-platform", "cf-mitigated",
    "enable javascript and cookies", "cf_chl_opt", "attention required",
)
TINY_ERROR = ("temporarily unavailable",)


def classify(title, body):
    low = (body or "").lower()
    if any(m in low for m in CHALLENGE_MARKERS):
        return "CHALLENGE"
    if any(m in low for m in TINY_ERROR):
        return "TEMP_UNAVAILABLE"
    if "primarydetailscontainer" in low or "| event | tapology" in (title or "").lower():
        return "CLEARED"
    if len(body or "") < 3000:
        return "TINY/ERROR"
    return "UNKNOWN"


def run(channel):
    label = channel or "bundled-chromium"
    out = {"channel": label}
    with sync_playwright() as pw:
        kw = dict(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
        if channel:
            kw["channel"] = channel
        try:
            browser = pw.chromium.launch(**kw)
        except Exception as e:
            out["launch_error"] = str(e)[:300]
            return out
        try:
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            resp = page.goto(URL1, wait_until="domcontentloaded", timeout=60000)
            out["http_status"] = resp.status if resp else None
            out["cf_header"] = (resp.headers.get("cf-mitigated") if resp else None)
            time.sleep(12)          # a managed challenge self-solves in ~8-12s
            body = page.content()
            out["title"] = page.title()
            out["bytes"] = len(body)
            out["verdict_nav"] = classify(out["title"], body)
            out["ua"] = page.evaluate("navigator.userAgent")
            out["webdriver"] = page.evaluate("navigator.webdriver")
            out["platform_js"] = page.evaluate("navigator.platform")
            out["hw_concurrency"] = page.evaluate("navigator.hardwareConcurrency")

            # the documented working primitive: same-origin fetch inside the cleared page
            if out["verdict_nav"] == "CLEARED":
                r = page.evaluate(
                    """async (u) => {
                        const res = await fetch(u, {credentials: 'include'});
                        const t = await res.text();
                        return {status: res.status, len: t.length, head: t.slice(0, 1200)};
                    }""", URL2)
                out["fetch_status"] = r["status"]
                out["fetch_bytes"] = r["len"]
                out["verdict_fetch"] = classify("", r["head"])
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        finally:
            browser.close()
    return out


if __name__ == "__main__":
    print(f"container arch: {platform.machine()}  python: {platform.python_version()}", flush=True)
    results = []
    for ch in ("chrome", None):
        print(f"\n>>> probing channel={ch or 'bundled-chromium'} (headed, under Xvfb)", flush=True)
        res = run(ch)
        print(json.dumps(res, indent=2), flush=True)
        results.append(res)
    print("\n===== SUMMARY =====", flush=True)
    for r in results:
        print(f"  {r['channel']:>18}  nav={r.get('verdict_nav', r.get('launch_error','n/a'))[:40]:<22} fetch={r.get('verdict_fetch','-')}", flush=True)
