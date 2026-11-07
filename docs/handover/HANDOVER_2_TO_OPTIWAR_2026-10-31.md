# lb-ai-glasses → Optiwar: AI Glasses patch 2 (31 Oct 2026)

**Patch:** `optiwar_ai_glasses_2.patch` (sha256 `d89e0bd5…935f`), against `main` @ `0bd21fb`. It applies cleanly with `git apply`.
Nothing was committed, pushed or deployed. The flag `AI_GLASSES_IN_ENABLED` stays **off**.

**Tests:** 1,729 pass locally (378 skipped, no test DB), with 7 new tests in `tests/test_ai_glasses.py`. Please run the full suite with the DB, then `deploy.py plan`.

## 1. Dispatch date no longer drifts past the promise (your item 1)
`models.success()` now uses `_ship = _aig or dispatch_date_obj().date()`. This is your suggested line.

## 2. Paid pre-orders keep their dispatch date (your item 2; the owner approved it today)
- **New table** `ai_glasses_order_dispatch` (order_line_id PK, order_id, product_id, sales_status, dispatch_date, sealed_at):
  - declared in `ai_glasses.TABLES` (`DISPATCH_SCHEMA`) and added to `deploy.py ai_glasses_tables()`, so `deploy.py migrate` creates it;
  - additive, `CREATE TABLE IF NOT EXISTS`, with no ALTER on `orders`.
  - We used a small table instead of a column on `orders`, so the large table needs no ALTER. The effect is the same.
- **Sealing at payment:** `paid_orders.seal_aig_dispatch(cursor, order_id)` runs in `apply_paid_order` (inside the payment transaction) and in `/test-checkout`.
  - It writes one row per AI Glasses line: the order date plus 30 days (PREORDER) or plus 7 days (SELLING_FAST). SOLD_OUT gets no row.
  - It uses `INSERT IGNORE`, so a second callback never changes a date.
  - It never raises, so the payment cannot fail because of it.
  - `paid_orders` stays free of Flask, so the 30/7 days are mirrored there. A test keeps them equal to `ai_glasses.PREORDER_DAYS` and `SHIP_DAYS[1]`.
- **Order page:** `success()` reads the sealed date first (`ai_glasses.promised_dispatch`). It falls back to the live status only when no date is sealed. The Google review survey date follows the same value.
- **Tested on the local DB:**
  - Order TWEEID-699814 (progressive Rx, ₹39,825) was sealed at 30 Nov 2026.
  - After the product was switched to SELLING_FAST, the page still showed **Monday, 30 November 2026**.
  - An unsealed order, placed before this change, moved to 7 Nov, as expected.
  - Production has no paid AI Glasses orders, so no backfill is needed.

## 3. Face Match: AI Glasses that fit (owner request)
The owner wants AI Glasses that match a customer's face to appear when they use Face Match ("Matching frames" in My Faces → `all-spectacle-frames.html?filter=facematch`).
- `ai_glasses.face_matches()` scores every AI Glasses size with the same `face_fit.evaluate` rule as frames, for the person being shopped for.
  - It keeps one card per style: the first size that fits. Sold-out models are left out.
- `all_spectacle_frames()` passes `aig_fit`, and `all_frames.html` includes the new `templates/ai_glasses/_face_match_strip.html`. It appears above the frame list as **"AI Glasses that fit <person>"**, with the fit label, size, dispatch badge, price, MRP and offer. Each card links to the AI Glasses product page.
- **When it shows:** only on optiwar.in, only when `ai_glasses.is_visible()` (flag on, or the owner's preview), only when signed in with a scan, and only while the Face Match filter is on. It is hidden when the filter is turned off.
- **Separate from frames:** AI Glasses never become frame cards and are never added to `/api/frames`, so "Buy Frame with My Lenses" (free lenses) never applies to them.
- **Checked locally:**
  - .in signed in: the strip shows the Large sizes as GOOD for the test face (145 mm).
  - .in signed out: no strip.
  - .com: no strip.
- `DEPLOY_SET` and `NEW_IN_RELEASE` include the new template.

## 4. Local test order: done
- Your `generate_random_order_id()` definition is installed locally.
- Test order HTZNRU-924670 checks:
  - `is_test=1`, India site, ₹39,825;
  - prescription linked (`rx_id`);
  - `order_face_snapshots` sealed (Large 53-22-155, GOOD);
  - stock still 999;
  - the success page shows the order.

## 5. Owner decisions for you (please action)
- **Test account:** the owner asks you to flag **the owner's Google account, lensebazaar@gmail.com,** as `customers.is_test=1` for production AI Glasses test orders.
  - While flagged, every order from that account is a test order: no payment, no stock, hidden from reports.
  - Please confirm with the owner whether it should be removed after the launch tests.
- **Face profiles:** noted. They are on for the owner only, so the owner's test orders will seal the face fit.

## Still open (unchanged)
- `generate_catalog.py` must skip SMART_EYEWEAR (release blocker).
- DOA-only returns (item F).
- The accountant's HSN and rate for the Rx lens line.
- The opticians' CYL/ADD/PD ranges.
- Refunds in the sold count.
- Final prices.
