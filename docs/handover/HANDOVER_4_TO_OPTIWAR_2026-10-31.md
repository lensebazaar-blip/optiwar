# lb-ai-glasses → Optiwar: AI Glasses patch 4 (31 Oct 2026)

**Patch:** `optiwar_ai_glasses_4.patch` (sha256 `ac725cb2…3709`), against `main` @ `ea494a8` (#238 merged). It applies cleanly with `git apply`.
Nothing was committed, pushed or deployed. The flag stays **off**.

**Tests:** 1,737 pass locally (378 skipped, no test DB), with 2 new tests in `tests/test_ai_glasses_returns.py`. Please run the full suite with the DB. No schema change, and no new files.

## Mixed orders keep the frame reasons (your point on #238; owner's choice)
An order holding AI Glasses **and** an Optiwar frame now offers both sets of reasons. AI Glasses can still be bought on the same order as frames.

**My Orders return request (`return_request.py`)**
| Order holds | Reasons offered |
|---|---|
| AI Glasses only | AI Glasses dead on arrival · Wrong item received (unchanged) |
| AI Glasses + a frame | AI Glasses dead on arrival · Manufacturing defect · Wrong item received · Damaged on arrival · Other |
| No AI Glasses | Unchanged |

- `_ai_glasses_kinds()` replaces `_has_ai_glasses()`. It returns `{order_id: "ai_glasses" | "mixed"}`; plain orders are absent.
- `reasons_for(kind)`, `form_context(kind)`, `_validate_form(form, kind)` and `_card(..., kind=)` take that kind.
- `ai_glasses_doa_only` is refused only on AI Glasses-only orders.
- Policy clause 13A still governs the AI Glasses in a mixed order at inspection.

**Checkout (`templates/checkout.html`)**
- A mixed cart now also keeps the "No change-of-mind returns" line for the frame, when the frame has no prescription lenses. Before this patch, the DOA notice hid that line.

**Checked locally:** the kind query returns AI-only for HTZNRU-924670, plain for a frame order, and mixed for a temporary two-line order. That order was rolled back.

## Test account
Noted: customer_id 498 (Google) and 579 (email code) are both flagged `is_test=1`. The owner will ask you to clear both after the launch tests.
