# AI Glasses on optiwar.in: handover to the Optiwar Devin agent (31 Oct 2026)

**From:** the LensBazaar AI Glasses team. Built and tested locally on a copy of `main` @ `0446975`.
**Nothing was committed, pushed or deployed.** Open it as a PR, CI must pass, and the owner merges and says "deploy N". The flag stays **off** until the owner switches it on.

## Files in this folder
| File | What it is |
|---|---|
| `optiwar_ai_glasses.patch` | The full change. Applies cleanly to `0446975` (`git apply`). |
| `ai_glasses_images.tar.gz` | Photos for the 10 products, `rayban-meta/<CODE>/<CODE>_1.png` (front) and `_2.png` (side), from the supplier linesheet. Extract into `static/catalog/` on the server. Not in git. |

## What the patch does
| Item | Change |
|---|---|
| A: vertical, page, preview | New `ai_glasses.py` blueprint. `SMART_EYEWEAR` vertical. `/ai-glasses` launch page and `/ai-glasses/<slug>` product page (`templates/ai_glasses/`, `static/ai-glasses/`). Visible only on optiwar.in, and only when `AI_GLASSES_IN_ENABLED=1` or with the owner's signed preview (7 days; `noindex` while the flag is off). An "AI Glasses" entry is added to the Lens Types bar when visible. |
| Gating everywhere else | `catalogue.catalogue_site_filter()` now always excludes `SMART_EYEWEAR`, so frame listings, search, the product APIs, feeds and sitemaps never return AI Glasses. `is_product_allowed()` refuses them unless visible on .in, so `/add_to_cart` and checkout refuse them too. Tests are updated to the new predicate. |
| B: identifiers | New table `ai_glasses_products` (product_id → family, style, frame/lens colour, size, brand, mpn = supplier SKU, gtin = EAN-13, rx_allowed, total-power range, sales_status). Declared in `ai_glasses.TABLES` and added to `deploy.py migrate` via `ai_glasses_tables()`. Also `ai_glasses_status_log`. |
| C: own lens set | Non-prescription (included) / Single vision ₹4,900 / Progressive ₹9,900 (owner's placeholder prices), in `ai_glasses.LENSES`. Optiwar's free lens and `lens_pricing.json` are untouched and never used for AI Glasses. |
| D: prescription | Server-side `validate_rx()`: SPH −12..+6, CYL −6..0, 0.25 steps, AXIS required with CYL, ADD +0.75..+3.50 for progressive, PD 50–80. Total power (SPH+CYL) uses the per-model manufacturer range (−12.00..+6.00 for Wayfarer Gen 2). Customers may also choose "send after ordering". Stored in `rx_collector` as for frames. |
| E: stock and status | `paid_orders`: AI Glasses lines are marked fulfilled with **no stock decrement**; quantity is fixed at 999. `sales_status` is PREORDER (dispatch date = today + 30), SELLING_FAST (3–7 days) or SOLD_OUT (add-to-bag blocked on page and server). JSON-LD availability: PreOrder / InStock / OutOfStock. |
| Ops API | `GET /ops/api/ai-glasses` lists products with `sold` (fulfilled, non-test, non-archived lines). `POST /ops/api/ai-glasses/<id>/status` takes `{"status": ...}` and needs `X-Ops-User`; changes are logged. `POST /ops/api/ai-glasses/preview-link` returns the owner's preview URL. All use `ops._require_ops_auth`. |
| AI chat | `ai_glasses.prompt_section()` is added to `chat.py` and `chat_gateway.py` prompts **only when visible**. It lists models, prices, statuses and lens prices, the facts the assistant may state, no free lens, the prescription range, DOA-only returns, and says to share the page link instead of `[PRODUCTS:]`. |
| J: EUR on .in | The `index.html` category offers now use the live INR frame range on .in (₹599–₹1,099 locally) and the unchanged EUR range on .com. **Note:** `templates/index.html` was not in `DEPLOY_SET`; the patch adds it. Please check production's copy for reviewed drift first. |
| I: products | `scripts/ai_glasses/build_import.py` writes `ai_glasses_import.sql`: 10 products (`sell_on_in=1`, `sell_on_com=0`, `show_in_listings=0`, qty 999, HSN 85176290, MRP plus 25% offer as `product_special_price`, slash-free codes such as `0RW4012601-7150`), the `ai_glasses_products` rows, and `tax_tables` rows 85176290 @18% and 90015000 @5%. Safe to re-run. |

**Tests:** 1,714 tests pass locally, including 12 new ones in `tests/test_ai_glasses.py`. 378 were skipped because there was no local test DB; please run the full suite in CI.

## Update 31 Oct (owner feedback)
- **AXIS:** 5–180 in steps of 5, the same as Optiwar frames, enforced on the page and on the server.
- **Face scan fit:**
  - `products.product_size` is now the full `50-22-150` / `53-22-155`, so `face_fit.evaluate` scores AI Glasses with the same rule as frames.
  - The product page shows the verdict per size for the person being shopped for (EXCELLENT / VERY GOOD / GOOD / Not a match, with reasons). Without a scan it shows a link to /tryon.
  - `face_cart.is_frame_line` accepts the `AI Glasses` category, so checkout shows the person and fit, and `order_face_snapshots` seals it with the order.
- **Test orders:** `/test-checkout` now also seals the face fit (`face_cart.record`), as the real checkout does.
- **Checkout summary:** `policy_terms.spectacle_summary` shows AI Glasses as glasses price plus lens option. It used to show "Frame Rs 0".
- **Questions for the team:**
  - Please send `SHOW CREATE FUNCTION generate_random_order_id`. It is not in the repo or the dump, so local test orders cannot get an order number.
  - Which Google account is flagged `customers.is_test` on production, for the owner's test orders?
  - Is `FACE_PROFILES_ENABLED` on in production? Fit is sealed on orders only when it is.
- **Not yet done:** item F, DOA-only returns in the return flow and policy text (needs a new declarations version). Checkout currently shows Optiwar's standard customized-spectacles return disclosure.

## Release checklist (owner approves each step)
1. **⚠️ Blocker:** `/root/catalog/generate_catalog.py` (server-only) must skip `SMART_EYEWEAR` while the flag is off. Separate owner approval. The flag cannot go on before this is done.
2. PR merged → `deploy.py migrate --confirm` creates the 2 tables → `deploy.py release --confirm`.
3. Apply `scripts/ai_glasses/ai_glasses_import.sql`, and extract `ai_glasses_images.tar.gz` into `static/catalog/`. Run the image pipeline; set `master_px` no larger than the source width.
4. **Ops invoice (eu.lensbazaar.com Ops):**
   - the AI Glasses line prints HSN **8517 62 90 at 18%**;
   - prescription lenses (`lens_price` on the line, details in `rx_collector`) print as a separate **9001 50 00 at 5%** line;
   - the invoice needs a per-line rate column, because one invoice can mix 18% and 5%.
5. Ops screen: a sales-status card using the Ops API above.
6. Owner preview: `POST /ops/api/ai-glasses/preview-link` → check `/ai-glasses` on optiwar.in.
7. One Razorpay test order on .in (owner-approved), then a refund.
8. Launch: set `AI_GLASSES_IN_ENABLED=1` (drop-in), restart, then check the page, chat, sitemap and AI catalogue.

## Not in this patch (phase 2, before .in is released)
- .in Google Merchant feed entries for AI Glasses (gtin, brand, mpn, preorder plus availability_date, sale price against MRP). The feed excludes them today.
- Sitemap rows for `/ai-glasses` pages (with the generate_catalog.py change).
- Sold count minus refunds: it currently counts fulfilled lines. Please tell us how refunds are recorded if they should be subtracted.

## Open with the owner
- Prescription lens prices ₹4,900 / ₹9,900 are placeholders. The CYL, ADD and PD ranges need opticians' confirmation.
- Product facts on the page and in the chat (12 MP, 3K, 8 hours, "Hey Meta", app) are as published by the manufacturer, and match the LensBazaar build.
