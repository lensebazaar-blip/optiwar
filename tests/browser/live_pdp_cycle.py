"""Run the owner's exact Saved-Rx acceptance sequence against a page rendered
by production (authenticated HTML dumped from the box), served locally so
the same JS runs with no reload between cycles. Usage:

    python tests/browser/live_pdp_cycle.py /tmp/pdp_498.html [cycles]
"""
import asyncio
import json
import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from saved_rx_apply_harness import READ  # noqa: E402

VIEWPORTS = {"desktop": {"width": 1366, "height": 900},
             "mobile": {"width": 390, "height": 844}}


def check(a, label, failures):
    for eye in ("right", "left"):
        if a[eye]["sph"] != "-0.50":
            failures.append("%s selector %s=%r" % (label, eye, a[eye]["sph"]))
        if "PWR -0.50" not in (a[eye]["summary"] or ""):
            failures.append("%s summary %s=%r" % (label, eye, a[eye]["summary"]))
    if not (a["state"]["right"] and a["state"]["left"]):
        failures.append("%s page state %r" % (label, a["state"]))
    if a["reused"] != "2" or a["failed"] or "R -0.50 · L -0.50" not in (a["note"] or ""):
        failures.append("%s note/reused %r %r" % (label, a["note"], a["reused"]))


async def run(page, cycles, vp_name, failures, beacons):
    await page.select_option('[name="right_sph"]', "-1.50")
    await page.select_option('[name="left_sph"]', "-1.50")
    for i in range(cycles):
        await page.click('[data-help="saved"]')
        await page.click('[data-role="use-saved"][data-cl-rx-id="2"]')
        check(await page.evaluate(READ), "%s c%d use" % (vp_name, i), failures)
        await page.select_option('[name="right_sph"]', "-2.00")
        await page.select_option('[name="left_sph"]', "-2.25")
        await page.click('[data-role="use-saved"][data-cl-rx-id="2"]')
        check(await page.evaluate(READ), "%s c%d reuse" % (vp_name, i), failures)
        await page.select_option('[name="right_sph"]', "")
        await page.select_option('[name="left_sph"]', "")
        await page.click('[data-role="use-saved"][data-cl-rx-id="2"]')
        check(await page.evaluate(READ), "%s c%d reset+use" % (vp_name, i), failures)


async def main():
    from playwright.async_api import async_playwright
    src, cycles = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 20
    d = os.path.dirname(os.path.abspath(src))

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: Quiet(*a, directory=d, **k))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/%s" % (server.server_address[1], os.path.basename(src))
    failures, beacons, errors = [], [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for vp_name, vp in VIEWPORTS.items():
            ctx = await browser.new_context(viewport=vp, is_mobile=vp_name == "mobile")
            page = await ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))

            async def route(r):
                if "/api/chat/dev-defect" in r.request.url:
                    beacons.append(json.loads(r.request.post_data or "{}"))
                    await r.fulfill(status=204, body="")
                elif r.request.url.startswith("http://127.0.0.1"):
                    await r.continue_()
                else:
                    await r.abort()
            await page.route("**/*", route)
            await page.goto(url)
            await run(page, cycles, vp_name, failures, beacons)
            print("%-8s %d cycles: %s" % (vp_name, cycles, "PASS" if not failures else "FAIL"))
            await ctx.close()
        await browser.close()
    server.shutdown()
    mism = [b for b in beacons if b.get("code") == "RX_SAVED_STATE_MISMATCH"]
    print("RX_SAVED_STATE_MISMATCH beacons: %d; JS errors: %d" % (len(mism), len(errors)))
    if failures or errors:
        print("\n".join((failures + errors)[:30]))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
