# lb-ai-glasses → Optiwar: AI Glasses patch 3 (31 Oct 2026)

**Patch:** `optiwar_ai_glasses_3.patch` (sha256 `1d95713f…1649`), against `main` @ `4b93805`. It applies cleanly with `git apply`.
Nothing was committed, pushed or deployed. The flag stays **off**.

**Tests:** 1,735 pass locally (378 skipped, no test DB), with 8 new tests in `tests/test_ai_glasses_returns.py`. Three exact-match disclosure tests in `test_policy_terms.py` gained the new key.
Please run the full suite with the DB. The return-request and reverse-pickup DB tests cover the changed `customer_cards`/`submit` paths.
No schema change. The changed files are already in `DEPLOY_SET`, and one new test file is added.

## 1. Clear hides the Face Match strip (your fix request)
`_face_match_strip.html` also hides the strip on a click of `#owFltReset`.

## 2. DOA-only returns (release blocker, item F)
- **Policy (`policy_terms.py`):** new clause **13A "AI Glasses (smart eyewear) — dead on arrival only"**, inserted before clause 14 of the .in returns policy. The acceptance sentence gains "AI Glasses are returnable only if dead on arrival (DOA)."
  - Both apply **only when `AI_GLASSES_IN_ENABLED=1`** (read from the environment, as the drop-in sets it). The .in returns version then becomes **`2026-10-31-in`** and is sealed as usual.
  - With the flag off, .in stays on the sealed `2026-10-03-in`, and nothing about AI Glasses appears on the Terms page.
  - .com is unchanged.
  - `returns_text()` and `acceptance_text()` are used by `current()` and `returns_html()`.
- **Checkout:** new disclosure `ai_glasses_doa_shown`, recorded in `order_terms_acceptance.disclosures` (JSON, no schema change).
  - A cart with AI Glasses shows "AI Glasses — returnable only if dead on arrival (DOA)".
  - The 50% lens-deduction notice now counts frames only, because it is about discretionary returns and AI Glasses have none.
  - The "No change-of-mind returns" line is not shown when the DOA notice is.
- **My Orders return request (`return_request.py`):**
  - An order containing AI Glasses offers only **"AI Glasses dead on arrival (do not switch on or work)"** and **"Wrong item received"**. The wrong-item reason is kept per clause 7.
  - The server refuses any other reason for such orders (`ai_glasses_doa_only`).
  - The 7-day window, declarations, fee and inspection are unchanged.
  - Other orders are unchanged.
- **Local check (flag on):**
  - the checkout showed the DOA notice and posted version `2026-10-31-in`;
  - test order XYYYWM-870000 was accepted;
  - the Terms page on .in shows clause 13A, and .com does not.

## 3. Refunds in the sold count
`ai_glasses.sold_counts()` leaves out orders with a **FULL** refund in `order_refunds` whose status is PENDING or PROCESSED. A partial refund names no line, so those pairs stay counted. The query was checked on the local DB.

## Owner decisions recorded today
- **Lens prices:** keep the placeholders (₹4,900 / ₹9,900). The owner gives final prices at the final upload.
- **GST:** AI Glasses at **18%** (HSN 8517 62 90). Prescription lenses at **5%** (HSN 9001 50 00), on a separate line.
- **Prescription ranges confirmed:** SPH −12.00 to +6.00, CYL 0 to −6.00, ADD +0.75 to +3.50, PD 50–80.

## Still open
- `generate_catalog.py` must skip SMART_EYEWEAR (release blocker, yours).
- `is_test` for lensebazaar@gmail.com: you found 2 accounts with that email. Please flag only the one the owner signs in to with Google, and confirm the customer_id to the owner.
- The Ops invoice lines (18% / 5% with a per-line rate column) and the Ops sales-status card, from the release checklist.
