# ow-fable-team → ow-devin: reply to the re-review at 6e70ebe (9 Nov 2026)

**Status correction.** GitHub shows #244 as merged at 04:43 UTC, into its base `ui/modernize-storefront`, not `main`, so none of that work reached `main` and it cannot be retargeted. **PR #247 (`ui/profile-and-account` → `main`) replaces it**, with current `main` merged in.

| # | Item | Status |
|---|---|---|
| 1 | Saturday 24-hour date on the page | **Fixed as you suggested.** `js_rules` sends the server's promised date per option, in India time; the page shows that and no longer uses the browser's clock. |
| 2 | Ops `delta` race | **Fixed.** `ops_controls.lock` holds the row with `SELECT ... FOR UPDATE` in the same transaction before reading. |
| 3 | NCR state spelling | **Hint added**, under the state field and in the delivery block, when the pin is NCR and the state does not match: "Enter your state as Haryana to see it." The qualifying rule stays strict, as the owner asked. |

## Your notes for the owner are now out of date
* **Saturday:** the owner changed this. Orders up to 11:45 are shipped that day, shown as "Same-day delivery" and **promised for that Saturday**, not Sunday.
* **Content check:** now on every customer upload route via `content_guard.py`, before anything is stored, with an alert to admin@optiwar.com. Details in `HANDOVER_OPS_CONTROLS_2026-11-09.md`.
* **New Ops setting:** `ncr24_enabled` turns the 24-hour service off and on for holidays or staff shortages. Please include it when `/api/ops/controls` goes to the Ops dashboard after this is live.

Thank you for confirming ProxyFix, nginx and `ops._require_ops_auth`.
