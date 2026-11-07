# Checkout: four steps on phones, mixed-order dispatch choice (7 Nov 2026, PR #244)

## Phones: Items → Address → Terms → Pay (`templates/checkout.html`, `static/css/ow-ui.css`)
* Step chips carry `data-step` 0–3; steps 2 and 3 share the third card (the payment column). `owMobGo(step)`
  puts `ow-mob-step-N` on `#owCheckout`; CSS shows only the policy block (folds open, consent box) on step 2
  and hides it on step 3, where a "Terms accepted · Change" line sits above the totals. Without an
  `acceptance_text` (no policy block) step 2 is skipped.
* The fixed bar labels: Confirm address → Read the terms → I agree, continue → Pay Rs X. The bar refuses
  step 3 until the consent box is ticked and refuses to pay with the "frames sooner" dispatch choice.
* Item rows on phones: 72 px real product image (`product_picture`, 160 px derivative), name, lens line, price
  at the right, qty + Remove; the person selector, fit, prescription photo upload, View details and Rx badge
  fold behind one "Options" button (`.ow-ck-more`, added by JS). The old trust row and "Price" label are hidden.
  Desktop keeps its three columns and all controls visible.

## Mixed orders: AI Glasses that ship later + frames that are ready
* `ai_glasses.mixed_dispatch(cursor, cart, usual)` → both dates when the cart has AI Glasses lines (`ai_glasses: 1`)
  *and* other lines *and* the AI Glasses date is later. The pay column then shows "Frames dispatch" and
  "AI Glasses (pre-order) dispatch" and a choice:
  * **Send everything together** (default, `dispatch_mode=TOGETHER`): one dispatch by the AI Glasses date;
  * **I want the frames sooner** (`SPLIT`): shows a Remove link per AI Glasses line (`/remove_from_cart/...`);
    paying is blocked until the customer removes them or switches back. Optiwar never part-ships.
* `POST /checkout` reads `dispatch_mode` and, for a mixed cart, writes `ai_glasses_order_dispatch_pref`
  (order_id PK, mode, hold_until = AI Glasses date, usual_date). `record_dispatch_preference` never raises.
* Ops see it: `ai_glasses.dispatch_note_for(cursor, order_id)` → "ONE DISPATCH: the customer chose to hold every
  item and receive the whole order together by <date>. No partial dispatch." It is printed on the order page
  (`success.html`, under the dispatch date) and in the order confirmation mail (customer + admin cc), and the
  row is in the DB for the Optiwar ops tooling (`SELECT * FROM ai_glasses_order_dispatch_pref WHERE order_id=…`).
* Schema: additive `CREATE TABLE IF NOT EXISTS` in `ai_glasses.TABLES`; `deploy.py` lists it with the other
  AI Glasses tables (`tests/test_deploy_migration` expects the label `ai_glasses_order_dispatch_pref (table)`).

## Tests
`tests/test_ai_glasses.py::MixedDispatchTest` (4): mixed detection, recording (and refusal of unknown modes /
missing table), the note text, wiring across checkout, models, mail, success and deploy. Full suite 1765 OK.
Local: `/__local/test-cart?pids=377&aig=1` builds a mixed cart.
