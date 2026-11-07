# Delivery options at checkout, optiwar.in only (7 Nov 2026, PR #244)

`shipping_options.py` (stdlib), `templates/checkout.html`, `models.py`, `paid_orders.py`, `static/css/ow-ui.css`.

## What the customer sees (Pay step, India storefront only)
| Option | Fee | Promise | Offered when |
|---|---|---|---|
| Standard delivery | free | order date + 5 days | always |
| Expedite | Rs 300 | order date + 1 day | any India address |
| 24-hour delivery (Delhi NCR) | Rs 1000 | order date + 1 day | pin starts 110 / 122 / 2010 / 2011 / 2013, or state Delhi, or Delhi / Noida / Gurgaon / Gurugram / Ghaziabad in the address |
A Sunday promise moves to Monday. An order carrying an AI Glasses pre-order gets Standard only. optiwar.com: untouched.
The options re-render as the address is typed or a saved address is picked; the fee updates the Grand Total and the
"Pay Rs X" bar. Under the options: "Paid delivery options include 18% GST and appear on your invoice as a separate service line."

## Where the money goes — **Devin: be very cautious with totals**
1. `POST /checkout` re-validates the posted `shipping_option` against the posted state / pin / address
   (`shipping_options.choose`); a page that showed an option is never trusted alone. The fee is added to
   `grand_total` **before** `total_amount` goes to Paytm / Razorpay, so the gateway order amount includes it.
2. The choice is stored in `order_shipping` (order_id PK, option_key, fee, promised_date, state, zipcode) right after the
   `orders` rows; the `orders` rows themselves are unchanged (no fee line, no stock impact).
3. `paid_orders.order_amount_minor` = SUM(orders.order_total) + order_shipping.fee. This is what the Razorpay
   verification compares the gateway amount against (`AMOUNT_MISMATCH` otherwise). If the table is missing (older
   release) it adds nothing, which would then mismatch a paid fee: **deploy the migration before the templates**.
4. `success.html` shows Grand Total + fee and the option's promised date; the order note (also in the confirmation
   mail) reads e.g. "EXPEDITE (Rs 300 paid): promised by Monday, 09 November 2026".
5. Anything else that totals an order from `orders` alone (the KET finance app, reports, refunds) **will not see the
   fee** until it reads `order_shipping`. Please check `ops_refunds`, settlement reconciliation and the finance export.

## Ops team
* Every paid delivery option must appear on the invoice as a **separate service line, 18% GST inclusive** (Rs 300 /
  Rs 1000 are tax-inclusive amounts; courier / express delivery services, SAC 9968). Do not add GST on top.
* Read the choice per order: `SELECT option_key, fee, promised_date FROM order_shipping WHERE order_id = ...`;
  the order page and the confirmation mail carry the same line.
* "24-hour (Delhi NCR)" orders must go out the same day; "Expedite" the next working day; both ahead of standard.

## Tests
`tests/test_shipping_options.py` (9): NCR rule by pin, state and city words; availability for NCR / other India /
global / pre-order; posted choice validation; promised dates; storage never raising; the gateway amount includes the
fee; DDL additive; wiring. `tests/test_deploy_migration` lists `order_shipping (table)`. Full suite 1786 OK.
