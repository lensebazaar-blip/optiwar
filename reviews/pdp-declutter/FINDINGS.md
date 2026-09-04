# PDP still cluttered post-PR #86 — BC/DIA and totals repeated

Reviewed the live preview (`optiwar.com/categories/contact-lenses/
precision1-daily-disposable-contact-lenses--30-pack?pid=1015...`, screenshot
on file) against `templates/_lens_eye_cards.html` on `main`. PR #86 fixed the
three PR #83 findings correctly — this is new: two pieces of information are
rendered more than once on the same page, which is the actual source of the
"cluttered" read, not a missing feature.

## 1. BC/DIA fact line duplicated per eye — and a third time in Specifications

**File:** `templates/_lens_eye_cards.html:142-149`, inside the `{% for eye in
eyes %}` loop (`:135`).
```jinja
{% if fixed.base_curve or fixed.diameter %}
<p class="ow-rx-facts">
  {% if fixed.base_curve %}<b>BC {{ fixed.base_curve|float|round(1) }} mm</b>{% endif %}
  ...
</p>
{% endif %}
```
Base curve and diameter are lens-level facts (`fixed_choices()` in
`lens_order.py` — Precision1 is made in exactly one BC and one DIA, same for
both eyes). Because this block sits inside the per-eye loop, it prints
**twice** — identical text, once under Right eye, once under Left — and then
a **third time** in the Specifications accordion table
(`product_page_lens.html:236-237`). Three renders of "BC 8.3 mm · DIA 14.2
mm" on one page is the concrete thing reading as clutter.

**Fix:** pull the fact line out of the per-eye loop. Render it once, above
the `{% for eye in eyes %}` block (e.g. directly under the "Choose your
prescription" heading) — it's genuinely useful right where the customer is
about to pick a power, just not per eye. Leave the Specifications table as
the second, expected mention (spec tables restating headline facts is normal
and not what's being flagged here).

## 2. Per-eye header total duplicates the Order Summary directly below

**File:** `templates/_lens_eye_cards.html:140` (markup) and the `update()`
function's `line.textContent = ...` (`:406-429`, JS).
```html
<span class="ow-rx-name" ...>{{ 'Right eye' ... }}<small>...</small></span>
<span class="ow-rx-line" data-role="line"></span>   <!-- "2 boxes · €30.22" -->
```
Every eye card's header live-shows "N boxes · €X.XX" — and the exact same
number is repeated immediately below in `.ow-rx-summary` (`:185-197`,
`summary-{{ eye }}` rows). The reference mockup you liked shows the total in
exactly one place (the summary panel), not inline in the card header too.

**Fix:** delete the `<span class="ow-rx-line" data-role="line">` element and
its `update()` write (`role(fieldset, 'line')` block, `:413-414` JS — already
`if (line)`-guarded, so removing the element breaks nothing). Order Summary
stays the single source of truth for per-eye totals.

## Lower-confidence, worth a look while in there

- The "Enter manually / Upload prescription / Use saved prescription / Ask
  Optiwar AI" method row (`:127-132`) sits directly above the eye cards at
  full visual weight (pill buttons, same size as primary controls) —
  contributes to the busy first impression even though it's not duplicated
  info. Consider a visually quieter treatment (smaller/lighter pills) so it
  doesn't compete with the actual eye cards right below it.
- `<p class="ow-rx-sub">` ("Untick an eye you are not ordering for. Each eye
  has its own power and its own boxes.") is more copy than the reference
  needed — it uses a bare "Select quantity" heading, no subtitle. Not
  incorrect, just adds a line of reading before the actual controls.

## Not in scope here

Nothing above touches `lens_order.py`, pricing, or validation — this is
`templates/_lens_eye_cards.html` (+ one row in `product_page_lens.html`'s
Specifications table context, unchanged) only, same scoping discipline as
PR #83/#86.
