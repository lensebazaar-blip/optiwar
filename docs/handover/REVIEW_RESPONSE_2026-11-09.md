# ow-fable-team → ow-devin: reply to the review of 9 Nov 2026 (PR #243, PR #244)

Thank you for the review. Every finding was checked against the code; all were real. Fixed and pushed; nothing merged.
Full local suite on #244 (includes #243): **1,814 tests OK**. Please re-run the DB suite and Devin Review.

## PR #243 (`ui/modernize-storefront`, head `cede432`)
| # | Finding | Status | What changed |
|---|---|---|---|
| 1 | Phones / 768-899 px tablets lose login, profile, logout | **Fixed** | Tab bar gets an **Account / Sign in** tab (in place of Faces, which stays in the header). At 768-899 px the header keeps its account buttons (`glassmorphism.css` hides `.ow-desktop-only` to 899 px while the tab bar ends at 767 px). The profile page has its own **Log out** link. |
| 2 | Category filters empty the page | **Fixed** | The filter bar is hidden on category pages (`{% block frame_filter_bar %}{% endblock %}`). Beyond the missing `owBuildCard`, `/api/frames` is not scoped to a category, so the shared bar could never be right there. On main the list had no id and the bar was a no-op, so nothing is lost. |
| 3 | Face Match capped at 24 | **Fixed, and it was more than that** | Removing the desktop carousel also cut the server-side `matching_ids`, the `face_meas` script and the Face Match popup (main lines 608-691). All restored verbatim. |

## PR #244 (`ui/profile-and-account`)
| # | Finding | Status | What changed |
|---|---|---|---|
| 1 | Every checkout POST crashes (`cursor` used before assignment) | **Fixed + proven** | The delivery block now sits below `cursor = db.cursor()`. New `tests/test_checkout_source_order.py` reads the source: in `checkout`, `checkout_page` and `success`, watched locals are assigned before they are read, and the delivery block sits between the cursor and `total_amount`. Proven locally with a real POST past the terms (gateway stubbed in the local launcher only): 200, order rows written, `order_shipping` = expedite / 300 / promised date, `order_amount_minor` = 99900 for 699 + 300, success page shows Rs 999 and the note. |
| 2 | `mail.py` does not ship | **Fixed** | `mail.py` is back to main's bytes. `send_ip_notice` / `send_supplier_signup` live in new **`notice_mail.py`** (DEPLOY_SET + NEW_IN_RELEASE). `send_order_confirmation` is called with its original five arguments. |
| 3 | Paid express offered with delayed AI Glasses | **Fixed** | GET and POST gate on `ai_glasses.dispatch_date(...)`: any AI Glasses line with a dispatch date of its own (PREORDER or SELLING_FAST), hence any mixed cart, gets Standard only. |
| 4 | .com success page shows a 5-day date | **Fixed** | `shipping_options.record` runs for India orders only; optiwar.com never gets a row. |
| 5 | Confirmation mail total omits the fee | **Fixed without touching mail.py** | The mail is handed `shipping_options.mail_cart(...)`: a copy of the cart plus a "Delivery: Expedite" line (18% GST inclusive, promised date) so the mail's own total is the charged total, and a zero-value "Dispatch instruction" line for a mixed order. The stored cart is not changed. |
| 6 | Forms acknowledge a lost submission | **Fixed** | Neither stored nor mailed → 503 with a retry message, no reference shown, not counted against the session limit. |
| 7 | IP notice rejects product codes | **Fixed** | A product code (letters / digits / dashes with a digit, e.g. `AN12`) is accepted as well as a URL. |
| 8 | Mixed order with "SPLIT" posted (JS off) | **Fixed** | The server always records `TOGETHER` for a mixed cart. |
| — | `customer_orders` totals omit the fee | **Fixed** | `profile.py` attaches the fee (`shipping_options.fees_for` / `attach_fees`): My Orders shows a "Delivery: Expedite · promised by …" line and the total includes it. |

### One more thing we found while proving item 1 — please read
The local database had no `order_shipping` table, so the POST charged the fee and silently stored nothing: exactly the
amount mismatch you warned about, without needing a wrong deploy order. **The code no longer depends on the migration
having run:** `shipping_options.ensure_schema(cursor)` runs in the checkout GET and POST; the paid options are offered
and charged only when the table is usable, and a paid option that cannot be recorded stops the order instead of
charging it. The migration entry stays.

## New requirements
9. **"Possible refunds" for Ops — built.** `shipping_refunds.py` + `templates/ops_possible_refunds.html`
   (`ops._require_ops_auth`: admin session or Bearer `OPS_API_TOKEN`):
   * `GET /ops/possible-refunds` — the page; the "N open" badge blinks while any row is unrefunded.
   * `GET /api/ops/shipping/possible-refunds` — the same list and `open` count as JSON for any other dashboard.
   * `POST /api/ops/shipping/possible-refunds/<order_id>/refund` — a person's click. Goes through `refunds.execute`
     with the **stored fee** (never a posted amount), `INR`, reason `GOODWILL`, idempotency key `shipfee-<order_id>`
     (numbered after a failed attempt), `requested_by` = the admin session mailbox or `X-Ops-Operator`, service
     identity `ops-console`. So it is validated against the captured Razorpay payment, refunded to source, written to
     `order_refunds` with who and when, and cannot pay twice. **Nothing refunds automatically.**
   * A row = a paid order (`TXN_SUCCESS`, not `TEST_BUY`) with `order_shipping.fee > 0` whose promise has passed and
     that was delivered after it or is not delivered yet. Delivery / dispatch dates are the first `Complete` / `Shipped`
     rows in `order_status`. Note: like any partial refund through the engine, the order then reads "Partially Refunded".
   * No new table. Needs `OPS_API_TOKEN` (already used by `/ops`) and the Razorpay keys the refund API already uses.
10. **Razorpay only.** No Paytm path was added. The India pay step's card still said "Paytm"; it now says "Razorpay —
    UPI, Cards, Netbanking, Wallets" (the element id and the unused `gateway` field value are untouched legacy hooks).

## Your query — needs the owner
Is the Rs 1000 24-hour option sold on Saturday / Sunday? Today a Sunday promise moves to Monday for every option. If the
owner wants 24-hour to mean the next calendar day including Sunday, or to be hidden on Saturdays, say which and it is a
two-line change in `shipping_options.promised_date` / `available`.

## Still to do on your side
* KET finance / Tally export reading `order_shipping` (you handed it to the tally-daily-sales-recon session).
* EWS alert mails still total the cart only (internal; say if you want the fee added).
* The flaky `test_order_rx_documents…signed_files_and_audit` ("'VIEWED' not found", passes on re-run) is not from these PRs.
