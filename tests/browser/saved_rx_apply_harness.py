"""Browser acceptance for the saved-prescription apply engine.

Renders the real ``_lens_eye_cards.html`` with a lens fixture, loads it in
Chromium (desktop and iPhone-size), and runs the owner's exact sequence:
start from an already-selected prescription, Use a saved one, assert the
selectors, the page state and the order summary; change the powers by hand,
Use again; Reset (untick/retick, clear); Use again — 20 cycles without a
reload, for both eyes, right only and left only. Any mismatch, any
RX_SAVED_* defect beacon or any JS error is a failure.

Run:  python tests/browser/saved_rx_apply_harness.py
Needs playwright (``pip install playwright && playwright install chromium``).
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from jinja2 import Environment, FileSystemLoader, select_autoescape

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import lens_order  # noqa: E402
import lens_rx  # noqa: E402

CYCLES = int(os.environ.get("SAVED_RX_CYCLES", "20"))

# Precision1 as production states it: RULES mode, one base curve, spheres.
SPH = ["%.2f" % (-0.25 * i) for i in range(2, 49)]
RULES_OPTIONS = {
    "mode": "RULES", "colors": [], "base_curves": ["8.30"], "tree": {},
    "lists": {"base_curve": [{"label": "BC 8.3", "value": "8.30"}],
              "sph": [{"label": s, "value": s} for s in SPH]},
}
RULES_FIXED = {"base_curve": "8.30", "diameter": "14.20"}

# A toric MATRIX lens: cylinder and axis depend on the sphere chosen.
MATRIX_OPTIONS = {
    "mode": "MATRIX", "colors": [], "base_curves": ["8.60", "8.90"],
    "lists": {},
    "tree": {"": {
        "8.60": {"-0.50": {"": {"axes": [], "adds": []},
                           "-0.75": {"axes": ["10", "180"], "adds": []},
                           "-1.25": {"axes": ["90"], "adds": []}},
                 "-1.50": {"": {"axes": [], "adds": []},
                           "-0.75": {"axes": ["180"], "adds": []}},
                 "-2.00": {"": {"axes": [], "adds": []}},
                 "-2.25": {"": {"axes": [], "adds": []}}},
        "8.90": {"-0.50": {"": {"axes": [], "adds": []}},
                 "-1.50": {"": {"axes": [], "adds": []}}}}},
}

LENS = {"product_id": 1015, "pack_quantity": 30, "sku": "CL-PRECISION1"}


def saved_entry(cl_rx_id, right, left):
    eyes = {"right": right, "left": left}
    summary = " · ".join(
        "%s SPH %s" % ("R" if e == "right" else "L", v["sph"])
        for e, v in eyes.items() if v)
    return {"cl_rx_id": cl_rx_id, "created_at": "2026-09-08",
            "summary": summary, "eyes": eyes}


def render(options, fixed, saved_rx, submitted=None, saved_loaded=None, ai_proposal=None):
    env = Environment(loader=FileSystemLoader(os.path.join(REPO, "templates")),
                      autoescape=select_autoescape(["html"]))
    env.globals["url_for"] = lambda name, **kw: "/" + name
    tpl = env.get_template("_lens_eye_cards.html")
    body = tpl.render(
        lens=LENS, options=options, fixed=fixed, minimums={
            "single": 12, "both": 6, "stated_single": 12, "stated_both": 6,
            "waived": False},
        box_price=15.11, errors=[], submitted=submitted or {},
        eyes=lens_order.EYES, max_boxes=lens_order.MAX_BOXES_PER_EYE,
        signed_in=True, saved_rx=saved_rx, saved_loaded=saved_loaded,
        saved_summary=(saved_loaded or {}).get("summary", ""),
        ai_proposal=ai_proposal)
    return "<!doctype html><html><body>%s</body></html>" % body


READ = """() => {
  const v = n => { const f = document.querySelector('[name="' + n + '"]'); return f ? f.value : null; };
  const inc = e => document.querySelector('[name="' + e + '_include"]').checked;
  const desc = e => { const r = document.querySelector('[data-role="summary-' + e + '"]');
                      return r.hidden ? null : r.querySelector('[data-role="summary-desc"]').textContent; };
  const note = document.querySelector('[data-role="loaded-note"]');
  return {right: {on: inc('right'), sph: v('right_sph'), cyl: v('right_cyl'), axis: v('right_axis'), bc: v('right_bc'), summary: desc('right')},
          left: {on: inc('left'), sph: v('left_sph'), cyl: v('left_cyl'), axis: v('left_axis'), bc: v('left_bc'), summary: desc('left')},
          state: window.owLensPageState(), reused: v('reused_from'),
          note: note.hidden ? null : note.textContent, failed: note.classList.contains('is-failed')};
}"""


def expect(actual, rx, cl_rx_id, label):
    problems = []
    for eye in ("right", "left"):
        want = rx.get(eye)
        got = actual[eye]
        if bool(want) != got["on"] or bool(want) != actual["state"][eye]:
            problems.append("%s include on=%s state=%s" % (eye, got["on"], actual["state"][eye]))
        if not want:
            if got["summary"] is not None:
                problems.append("%s summary shown for excluded eye" % eye)
            continue
        for k in ("sph", "cyl", "axis"):
            if k in want and want[k] and got[k] is not None and float(got[k]) != float(want[k]):
                problems.append("%s %s=%s wanted %s" % (eye, k, got[k], want[k]))
        if got["summary"] is None or ("PWR " + want["sph"]) not in got["summary"]:
            problems.append("%s summary=%r" % (eye, got["summary"]))
    if actual["reused"] != str(cl_rx_id):
        problems.append("reused_from=%r" % actual["reused"])
    if not actual["note"] or actual["failed"] or "Loaded your saved prescription" not in actual["note"]:
        problems.append("note=%r failed=%s" % (actual["note"], actual["failed"]))
    return ["%s: %s" % (label, p) for p in problems]


async def run_case(browser, html_path, viewport, entry, other, label):
    ctx = await browser.new_context(viewport=viewport)
    page = await ctx.new_page()
    beacons, errors = [], []
    page.on("pageerror", lambda e: errors.append(str(e)))

    async def route(r):
        if "/api/chat/dev-defect" in r.request.url:
            beacons.append(json.loads(r.request.post_data or "{}"))
            await r.fulfill(status=204, body="")
        else:
            await r.continue_()
    await page.route("**/*", route)
    await page.goto(html_path)
    failures = []
    use = page.locator('[data-role="use-saved"][data-cl-rx-id="%d"]' % entry["cl_rx_id"])
    await page.click('[data-help="saved"]')

    async def set_manual(right, left):
        for eye, sph in (("right", right), ("left", left)):
            box = page.locator('[name="%s_include"]' % eye)
            if sph is None:
                if await box.is_checked():
                    await box.uncheck()
                continue
            if not await box.is_checked():
                await box.check()
            bc = page.locator('select[name="%s_bc"]' % eye)
            if await bc.count() and sph:
                await bc.select_option(entry["eyes"][eye or "right"]["bc"]
                                       if entry["eyes"].get(eye) else "8.60")
            await page.select_option('[name="%s_sph"]' % eye, sph)

    for cycle in range(1, CYCLES + 1):
        # Start: a different prescription already active on the cards.
        await set_manual("-1.50", "-1.50")
        await use.click()
        failures += expect(await page.evaluate(READ), entry["eyes"], entry["cl_rx_id"],
                           "%s c%d use-over-active" % (label, cycle))
        # Change by hand, Use the same saved prescription again.
        await set_manual("-2.00", "-2.25")
        await use.click()
        failures += expect(await page.evaluate(READ), entry["eyes"], entry["cl_rx_id"],
                           "%s c%d use-over-edited" % (label, cycle))
        # Use the other saved prescription, then this one again.
        await page.click('[data-role="use-saved"][data-cl-rx-id="%d"]' % other["cl_rx_id"])
        failures += expect(await page.evaluate(READ), other["eyes"], other["cl_rx_id"],
                           "%s c%d use-other" % (label, cycle))
        await use.click()
        failures += expect(await page.evaluate(READ), entry["eyes"], entry["cl_rx_id"],
                           "%s c%d use-after-other" % (label, cycle))
        # Reset: clear the powers, then Use again.
        await set_manual("", "")
        await use.click()
        failures += expect(await page.evaluate(READ), entry["eyes"], entry["cl_rx_id"],
                           "%s c%d use-after-reset" % (label, cycle))
    rx_beacons = [b for b in beacons if str(b.get("code", "")).startswith("RX_SAVED")]
    if rx_beacons:
        failures.append("%s: RX_SAVED beacons %r" % (label, rx_beacons))
    if errors:
        failures.append("%s: js errors %r" % (label, errors))
    await ctx.close()
    return failures, len(rx_beacons)


async def bootstrap_case(browser, html_path, entry, label):
    """?saved= on a fresh load: the note appears only after read-back."""
    ctx = await browser.new_context()
    page = await ctx.new_page()
    beacons = []

    async def route(r):
        if "/api/chat/dev-defect" in r.request.url:
            beacons.append(json.loads(r.request.post_data or "{}"))
            await r.fulfill(status=204, body="")
        else:
            await r.continue_()
    await page.route("**/*", route)
    await page.goto(html_path)
    failures = expect(await page.evaluate(READ), entry["eyes"], entry["cl_rx_id"], label)
    await page.reload()
    failures += expect(await page.evaluate(READ), entry["eyes"], entry["cl_rx_id"], label + " reload")
    await ctx.close()
    return failures, len([b for b in beacons if str(b.get("code", "")).startswith("RX_SAVED")])


async def incompatible_case(browser, html_path, label):
    """A saved value the lens is not made in: no success message, one defect."""
    ctx = await browser.new_context()
    page = await ctx.new_page()
    beacons = []

    async def route(r):
        if "/api/chat/dev-defect" in r.request.url:
            beacons.append(json.loads(r.request.post_data or "{}"))
            await r.fulfill(status=204, body="")
        else:
            await r.continue_()
    await page.route("**/*", route)
    await page.goto(html_path)
    await page.click('[data-help="saved"]')
    await page.click('[data-role="use-saved"][data-cl-rx-id="99"]')
    actual = await page.evaluate(READ)
    failures = []
    if not actual["failed"] or "could not be applied" not in (actual["note"] or ""):
        failures.append("%s: note=%r" % (label, actual["note"]))
    if actual["reused"] != "":
        failures.append("%s: reused_from kept %r" % (label, actual["reused"]))
    codes = [b.get("code") for b in beacons]
    if "RX_SAVED_STATE_MISMATCH" not in codes:
        failures.append("%s: defect codes %r" % (label, codes))
    for b in beacons:
        if "-9.75" in json.dumps(b):
            failures.append("%s: a power reached the defect channel" % label)
    await ctx.close()
    return failures


async def unsupported_field_case(browser, html_path, label):
    """A saved prescription stating a parameter this lens has no field for
    (a cylinder on a spherical lens) is not applied and not called loaded."""
    ctx = await browser.new_context()
    page = await ctx.new_page()
    beacons = []

    async def route(r):
        if "/api/chat/dev-defect" in r.request.url:
            beacons.append(json.loads(r.request.post_data or "{}"))
            await r.fulfill(status=204, body="")
        else:
            await r.continue_()
    await page.route("**/*", route)
    await page.goto(html_path)
    await page.click('[data-help="saved"]')
    await page.click('[data-role="use-saved"][data-cl-rx-id="98"]')
    actual = await page.evaluate(READ)
    failures = []
    if not actual["failed"] or "could not be applied" not in (actual["note"] or ""):
        failures.append("%s: note=%r" % (label, actual["note"]))
    if actual["reused"] != "":
        failures.append("%s: reused_from kept %r" % (label, actual["reused"]))
    where = " ".join(b.get("where", "") for b in beacons
                     if b.get("code") == "RX_SAVED_STATE_MISMATCH")
    if "right_cyl" not in where:
        failures.append("%s: mismatch not attributed to right_cyl: %r" % (label, beacons))
    await ctx.close()
    return failures


async def manual_edit_case(browser, html_path, label):
    """After Use, a power the customer changes by hand survives a later
    base-curve change: the dependency chain must not restore the saved one."""
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(html_path)
    await page.click('[data-help="saved"]')
    await page.click('[data-role="use-saved"][data-cl-rx-id="6"]')
    a = await page.evaluate(READ)
    failures = []
    if a["right"]["sph"] != "-0.50" or a["right"]["cyl"] != "-0.75":
        failures.append("%s: Use did not land %r" % (label, a["right"]))
    await page.select_option('[name="right_sph"]', "-1.50")
    await page.select_option('[name="right_bc"]', "8.90")
    a = await page.evaluate(READ)
    if a["right"]["sph"] != "-1.50" or "PWR -1.50" not in (a["right"]["summary"] or ""):
        failures.append("%s: saved power came back over the manual one: %r" % (label, a["right"]))
    await ctx.close()
    return failures


async def ai_case(browser, html_path, label):
    """Ask AI regression: the proposal the server rendered reaches the cards
    through the same engine, stays editable, and is never called "saved"."""
    ctx = await browser.new_context()
    page = await ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    await page.goto(html_path)
    a = await page.evaluate(READ)
    failures = []
    if not (a["right"]["on"] and a["left"]["on"] and a["right"]["sph"] == "-3.75"
            and a["left"]["sph"] == "-2.50" and "PWR -3.75" in (a["right"]["summary"] or "")
            and "PWR -2.50" in (a["left"]["summary"] or "")):
        failures.append("%s: cards %r" % (label, a))
    if a["note"] is not None or a["reused"] != "":
        failures.append("%s: saved note/reused set on an AI proposal" % label)
    if not await page.locator('[data-role="ai-note"]').is_visible():
        failures.append("%s: AI note missing" % label)
    if await page.locator('[name="rx_source"]').get_attribute("value") != "AI_ASSISTED_CONFIRMED":
        failures.append("%s: rx_source" % label)
    await page.select_option('[name="right_sph"]', "-4.00")
    a = await page.evaluate(READ)
    if a["right"]["sph"] != "-4.00" or "PWR -4.00" not in (a["right"]["summary"] or ""):
        failures.append("%s: not editable after proposal" % label)
    if errors:
        failures.append("%s: js errors %r" % (label, errors))
    await ctx.close()
    return failures


async def main():
    from playwright.async_api import async_playwright
    both = saved_entry(2, {"sph": "-0.50", "bc": "8.30"}, {"sph": "-0.50", "bc": "8.30"})
    right = saved_entry(3, {"sph": "-3.75", "bc": "8.30"}, None)
    left = saved_entry(4, None, {"sph": "-2.50", "bc": "8.30"})
    other = saved_entry(5, {"sph": "-4.00", "bc": "8.30"}, {"sph": "-1.00", "bc": "8.30"})
    toric = saved_entry(6, {"sph": "-0.50", "cyl": "-0.75", "axis": "180", "bc": "8.60"},
                        {"sph": "-1.50", "cyl": "-0.75", "axis": "180", "bc": "8.60"})
    toric_other = saved_entry(7, {"sph": "-0.50", "cyl": "-1.25", "axis": "90", "bc": "8.60"}, None)
    bad = saved_entry(99, {"sph": "-9.75", "bc": "8.30"}, None)
    bad["eyes"]["right"]["sph"] = "-99.00"
    cyl_on_sphere = saved_entry(98, {"sph": "-0.50", "cyl": "-0.75", "bc": "8.30"}, None)

    tmp = tempfile.mkdtemp()
    # Served over http so the defect beacon is a same-origin request the
    # harness can intercept and count (a file:// page cannot send one).
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
    handler = lambda *a, **kw: Quiet(*a, directory=tmp, **kw)  # noqa: E731
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d/" % server.server_address[1]
    rules = base + "rules.html"
    with open(os.path.join(tmp, "rules.html"), "w") as fh:
        fh.write(render(RULES_OPTIONS, RULES_FIXED, [both, right, left, other, bad, cyl_on_sphere]))
    matrix = base + "matrix.html"
    with open(os.path.join(tmp, "matrix.html"), "w") as fh:
        fh.write(render(MATRIX_OPTIONS, {"diameter": "14.50"}, [toric, toric_other]))
    boot = base + "boot.html"
    with open(os.path.join(tmp, "boot.html"), "w") as fh:
        fh.write(render(RULES_OPTIONS, RULES_FIXED, [both, other],
                        submitted=lens_rx.saved_form(both), saved_loaded=both))

    ai = base + "ai.html"
    with open(os.path.join(tmp, "ai.html"), "w") as fh:
        fh.write(render(RULES_OPTIONS, RULES_FIXED, [both, other], ai_proposal={"ok": True},
                        submitted={"right_sph": "-3.75", "right_bc": "8.30",
                                   "left_sph": "-2.50", "left_bc": "8.30"}))

    desktop = {"width": 1366, "height": 900}
    iphone = {"width": 390, "height": 844}
    failures, mismatches = [], 0
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for vp_name, vp in (("desktop", desktop), ("mobile", iphone)):
            for entry, label in ((both, "both"), (right, "right-only"), (left, "left-only")):
                f, m = await run_case(browser, rules, vp, entry, other, "%s %s" % (vp_name, label))
                failures += f
                mismatches += m
                print("%-8s %-10s %d cycles: %s" % (vp_name, label, CYCLES, "PASS" if not f else "FAIL"))
            f, m = await run_case(browser, matrix, vp, toric, toric_other, "%s toric-matrix" % vp_name)
            failures += f
            mismatches += m
            print("%-8s %-10s %d cycles: %s" % (vp_name, "toric", CYCLES, "PASS" if not f else "FAIL"))
        f, m = await bootstrap_case(browser, boot, both, "bootstrap ?saved=")
        failures += f
        mismatches += m
        print("bootstrap ?saved= + reload: %s" % ("PASS" if not f else "FAIL"))
        f = await incompatible_case(browser, rules, "incompatible")
        failures += f
        print("incompatible saved value -> no success, defect logged: %s" % ("PASS" if not f else "FAIL"))
        f = await unsupported_field_case(browser, rules, "unsupported field")
        failures += f
        print("saved cylinder on a spherical lens -> no success, right_cyl named: %s" % ("PASS" if not f else "FAIL"))
        f = await manual_edit_case(browser, matrix, "manual edit after Use")
        failures += f
        print("manual power survives a base-curve change after Use: %s" % ("PASS" if not f else "FAIL"))
        f = await ai_case(browser, ai, "ask-ai proposal")
        failures += f
        print("Ask AI proposal -> cards, editable, no saved claim: %s" % ("PASS" if not f else "FAIL"))
        await browser.close()
    server.shutdown()
    print("RX_SAVED_STATE_MISMATCH beacons in the passing runs: %d" % mismatches)
    if failures:
        print("\n".join(failures[:40]))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
