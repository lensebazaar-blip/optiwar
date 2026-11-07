# lb-ai-glasses → Optiwar: storefront UI modernisation (7 Nov 2026)

**Branch:** `ui/modernize-storefront`, built on `main` @ `ea494a8` (#238 merged). Opened as a draft PR for the Optiwar Devin agent to review, run CI on and merge on the owner's word. Nothing deployed.

## What this is
A mobile-first UI/UX pass over the customer-facing storefront, including the AI Glasses pages, following the UI/UX team conventions (per-page CSS under `static/css/`, `?v=` cache keys, `DEPLOY_SET` entries in the same PR, no third-party scripts, `_()` kept on customer strings, Lighthouse baselines under `docs/lighthouse/`). Backend, routes, forms, JSON-LD and the chat/voice widgets are untouched.

## Files
| File | Change |
|---|---|
| `static/css/ow-ui.css` (new, ~400 lines) | Design tokens, header, lens-type bar, mobile bottom tab bar, product-card grid, home sections, product page, auth pages, footer. Loaded last from `base.html` (`?v=2`). |
| `templates/base.html` | Links the new stylesheet; AI Glasses chip gets `lens-widget--aig`; new `<nav class="ow-tabbar">` (phones only: Home, Frames, AI Glasses or Lenses, Faces, Cart with badge). Footer markup unchanged, restyled. |
| `templates/index.html` | New content block: hero (price-led headline, two CTAs, proof points, product carousel), trust strip, shop-by-shape tiles, AI Glasses promo (only when `ai_glasses_nav`), lens-upgrade price grid, collection grid, reviews. All JSON-LD blocks unchanged. `%` inside `_()` is escaped as `%%` (gettext format char). |
| `templates/all_frames.html` | Desktop one-card-at-a-time carousel (CSS + JS) removed; the list is a responsive grid on every width (2 / 3 / 4 / 5 columns). Heading in `.ow-listing-head`. Card markup kept (same classes, forms, data-* hooks, favourites, zoom, infinite scroll, face-match badge, filters); only the two CTA labels and the "includes" line text changed. |
| `templates/categories.html` | Same heading; `<ul id="frames-list">` so category pages share the grid and card styling. |
| `templates/product_page.html` | Brand/code row and "Eyewear" chip removed (title carries the code), low-stock image badge removed (the chip under the price says it once), "Measure your Face" is a text link, filter bar block emptied; form ids `pdpFormPlano` / `pdpFormRx`; phones get a fixed buy bar (`#pdpStickyBar`: price, "Frame only", "Add my lenses") that submits the existing forms via `form=`. Frames only, in stock only. `<div class="pdp-cta">` unchanged (test_lens_order asserts on it). |
| `models.py` (`index`) | Home query capped at 48 random products (was every in-stock product, 4.7 MB HTML / 660 KB gzipped); render time passed as `hero_ts`. |
| `static/scripts.js` | Hero: random start slide, cached-copy redraw from productsData; home card labels: "Frame Only" / "Buy with My Lenses"; includes line text. No logic change. |
| `static/ai-glasses/aig.css` (`?v=4`) | Phone refinements: hero sizing, 2-up cards at 480–680px, sticky Add-to-bag above the tab bar on the product page. |
| `static/header-clean.css`, `static/homepage.css` | Deleted; referenced by no template (confirmed in the 14 Oct replies note). |
| `deploy/deploy.py` | `DEPLOY_SET` += `static/css/ow-ui.css`, `templates/categories.html`. |
| `docs/lighthouse/*.baseline-local-2026-11-07.json` | Mobile Lighthouse baselines (home, listing, PDP, checkout) taken on the local build before the change. Local numbers, not production. |
| `docs/screenshots/ui-modernize/*.jpg` | Before/after, 390 px and desktop. |

## Link previews (WhatsApp, Facebook, LinkedIn)
The live product page's `og:url` was the site root, so preview crawlers canonicalised to the homepage, which is 4.9 MB on production, and gave up (bare "optiwar.in" card). This PR sets `og:url` to the product URL and caps the homepage at 48 products. WhatsApp caches previews per URL for days; after deploy, test with a fresh query string (e.g. `&s=1`) or a product not shared before.

## Behaviour notes for review
- Chat launcher (`.ow-chat-btn`) is moved up on phones to clear the tab bar, and further on the frame PDP (`body:has(#pdpStickyBar)`); its classes are untouched.
- The tab bar hides the header cart/account icons on phones (they are in the bar); desktop header unchanged.
- The listing's `loadMore` still calls `owCarouselReinit` behind a `typeof` guard; the function no longer exists, so it is a no-op.
- Screenshots use the catalog master JPGs from the May-2026 site backup; the local launcher (outside the repo) ignores the derivative manifest because the AVIF/WebP derivatives exist only on production.

## Tests
`python -m unittest discover -s tests` — see the PR description for the count (same suite as #238; 378 skip without a test DB). Please run the full suite with the DB in CI.

## Not in this PR
- Checkout, profile, success and lens pages keep their inline CSS; they inherit tokens, font and footer only.
- No translations added for the new strings (English falls through); run the pybabel extract/update/compile cycle if `.po` coverage is wanted before deploy.
