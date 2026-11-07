# Optiwar → Devin: what to pull (kept current by the LensBazaar team)

Everything is on github.com/lensebazaar-blip/optiwar. Nothing is deployed; the owner says "deploy".

| # | Item | Where | State | What Devin does |
|---|---|---|---|---|
| 1 | Storefront UI modernisation (home, listing, PDP, auth, AI Glasses pages, footer, tab bar) + home payload cap + hero captions | PR **#243**, branch `ui/modernize-storefront` (`main` @ cd8a1ba merged in, after #239–#242) | Draft until the owner approves; tests 1745 OK locally | Run CI with the DB, Devin Review, merge on the owner's word, then `deploy.py release --confirm` on "deploy 243". New files to land: `static/css/ow-ui.css`, `templates/categories.html` are already in `DEPLOY_SET`. |
| 2 | AI Glasses patch 4: mixed-order return reasons (`return_request.py`, `checkout.html`, 2 tests) | branch `aig-patch-4-wip` (1 commit on `main` @ ea494a8; rebases cleanly), same change as `handover/optiwar_ai_glasses_4.patch` | Ready; owner's choice already given (see HANDOVER_4) | Open as a PR (or `git apply` the patch), CI, merge. Independent of #1; #1 does not touch those files. |
| 3 | Account pages: favourites as listing-style cards with a real empty state; guest order lookup (`/my_orders`) rebuilt without the Font Awesome CDN; profile tab labels on phones; phone country code from the site, not the hostname | PR **#244**, branch `ui/profile-and-account`, stacked on `ui/modernize-storefront` (base retargets to `main` once #243 merges) | Draft until the owner approves | After #243: CI, Devin Review, merge on the owner's word. `templates/my_orders.html` added to `DEPLOY_SET`. |

Not for the repo (local build only, in `optiwar_ai_glasses/` beside the checkout): `local_app.py` (launcher), `local_derivatives.py` (builds the AVIF/WebP/JPEG ladder from the May-2026 catalog backup), `run_local.sh`, `.env.local`.

Order: #2 can merge any time; #1 after the owner's approval. If #2 merges first, #1 rebases cleanly (no overlapping files).
