# "My Own Frame Fittings" — design reference for optiwar.in

Static HTML/CSS/JS mockup, no backend. Same status as the earlier LensBazaar
handoff: a **design + interaction reference**, not a drop-in template — the
markup style, ids, and route names are this mockup's own, not `optiwar`'s.

File: `optiwar-frame-fitting.html` (same directory as this doc).

## What it is

A new profile tab (`My Frames`, alongside the existing Account / Addresses /
Orders / My Face) for customers who already own a frame and want Optiwar to
fit prescription lenses into it — no new frame purchased. **`.in` only**:
`.in` sells eyewear exclusively (`catalogue.py: SITE_VERTICALS`), so this is
the whole of `.in`'s lens-pricing surface, not a variant of something else.

## Flow

1. **New Request** — up to 5 frames per request, each a collapsible card:
   labelled by the customer ("My Rayban Specs"), a labelling photo, a free-text
   description, a full per-eye prescription, and the real lens-pricing
   configurator (below).
2. Pickup address (reuses the existing Addresses tab's address list — mocked
   here as two static entries).
3. Itemized disclosure + single consent checkbox — liability, the weight
   surcharge, and the mid-flow billing gate (below), all as one agreement.
4. One payment upfront — pickup fee + all frames' estimated lens costs.
5. **My Requests** — tracked per request through an 8-stage timeline, with a
   conditional **Additional Billing** stage that must be settled before the
   frame is sent on to the lab.

## The pricing configurator — reused from the real engine, not invented

The first version of this mockup used a flat "pick one of 5 lens types"
picker. That was wrong: `models.py`'s `add_to_cart_with_lenses` (confirmed
live via `optiwar.com/add_prescription`, screenshots on file) computes lens
price from **three independent things**, and this mockup now mirrors that
shape exactly, minus the frame-price terms (there's no Optiwar frame in this
flow, so `product_special_price` / `spectacle_frame_with_complimentary_price`
don't apply here):

1. **Base tier, from PWR** (`models.py:2391-2406`) — no ADD power: highest
   |PWR| across both eyes ≥ **8.00 D** → **High Power Surcharge, +₹400 /
   +€4**; below that → complimentary. ADD power present → complimentary base,
   Bifocal/Progressive *style* priced separately (below).
2. **Cylinder surcharge, independent of the above** (`:2409-2415`): highest
   |CYL| ≥ 2.00 → **+₹400/+€4**; > 4.00 → **+₹1000/+€10**.
3. **Three optional addon categories** (`get_addon_price_map`, `:2267-2303`):
   **Coating** (Anti-Reflection ₹50, Blue Anti-Glare ₹100, Multi-Coated
   ₹200), **Thickness** (Thin ₹100, Ultra-Thin ₹350 — *and Photochromic
   Grey/Brown live in this category*, ₹350/₹650, not a category of their
   own), **Bifocal/Progressive style** (KT ₹250, D Flat-Top ₹500, Progressive
   ₹1000). All three are the code's real fallback defaults.

Server-side, choosing a Bifocal/Progressive style swaps the *other two*
categories to a different price context (`when_bifocal` / `when_progressive`
in `get_addon_price_map`) — screenshot 2 shows this live (coating drops to a
flat bundled rate). The mockup reproduces the mechanic with a
`BUNDLE_ADDON_PRICES` map and a visible "Bundle pricing applied" note, but
**the numbers in it are placeholders** — the real values live in
`/var/www/flask-optiwar-ow-release-090525/lens_pricing.json` on the box,
admin-editable, not in git, and not something this mockup could read. Every
price this mockup cannot verify against that file is marked with `*` in the
UI (bundle-swapped prices, and the two Photochromic lines).

Server-side recompute discipline in the real code — client-submitted price
fields are stored but `server_lens_price`/`server_total_price` (recomputed
from raw PWR/CYL/ADD) are what's actually charged — is the same pattern
`lens_order.validate_detailed` uses for contact lenses. Whoever builds this
needs the equivalent: never trust the client's addon totals, recompute from
the submitted Rx and addon codes server-side.

## The billing gate (from the 4 Sep brief)

- Reverse pickup: **₹500 base, up to 500g**; every extra 500g (or part) once
  weighed on receipt adds **₹200** — both admin-editable, not hardcoded (see
  `PICKUP_BASE_FEE` / `BASE_WEIGHT_G` / `EXCESS_SURCHARGE` constants in the
  file — same "must come from an admin settings row" flag as the lens addon
  prices above).
- Liability for loss/damage in transit is the customer's/carrier's, not
  Optiwar's — stated once, in the itemized disclosure, not buried in a link.
- Final pickup weight *and* final lens pricing are confirmed only after the
  frame is received and weighed; any amount over the submission-time estimate
  is billed separately as **Additional Billing**, a tracker stage that gates
  the frame from proceeding to the lab until settled.
- EWS: Email, WhatsApp, SMS at every tracker stage — surfaced as a single
  notice at the top of each tracked request, not per-line noise.

## What this mockup does not resolve

- Real addon and bundle pricing (needs `lens_pricing.json` off the box, or
  whoever owns it exporting current values).
- Whether Coating/Thickness/Bifocal selection should be per-frame (built this
  way, confirmed) also needs a real minimums/validation pass once it's wired
  to actual inventory — e.g. can Photochromic pair with Progressive server-side,
  or does the real matrix forbid some combinations this mockup lets you pick.
- Admin UI for `PICKUP_BASE_FEE`/`BASE_WEIGHT_G`/`EXCESS_SURCHARGE` — flagged,
  not built; same class of change as the existing lens addon price admin.
- Real address list / real pickup courier integration (AWB numbers here are
  fake).

## Suggested mapping, same shape as the Precision1 handoff

| Mockup concept | Likely real counterpart |
|---|---|
| `My Frames` tab | new block in `templates/profile.html`, alongside the existing 4 |
| Per-frame Rx + addon form | new route, mirrors `add_to_cart_with_lenses` minus frame-price terms |
| `get_addon_price_map()` | reused as-is — same JSON, same context-swap logic |
| Pickup fee / weight surcharge | new admin-editable settings, own table/row |
| 8-stage tracker + Additional Billing gate | new order-status model; EWS hooks wherever order status already changes elsewhere in the app |
