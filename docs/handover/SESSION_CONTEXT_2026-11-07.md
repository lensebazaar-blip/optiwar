# Session context for whoever continues this work (written 7 Nov 2026)

Start here. This is everything a new Claude Code session (cloud, on another machine, or on the Optiwar server) or Devin needs to carry on without the laptop this was built on.

## State of the work
- **PR #243** `ui/modernize-storefront` (draft): mobile-first storefront UI, with `main` @ cd8a1ba merged in (PRs #239–#242, the /search chat retirement). History is append-only: merge, not rebase, so two sessions can share the branch. Tests: 1745 OK locally (380 skipped without DB). Lighthouse mobile home 57 → 97. Full change list: `HANDOVER_UI_MODERNIZE_2026-11-07.md`. Before/after screenshots: `docs/screenshots/ui-modernize/`.
- **Branch `aig-patch-4-wip`**: AI Glasses mixed-order return reasons, same as `optiwar_ai_glasses_4.patch` in `HANDOVER_4_TO_OPTIWAR_2026-10-31.md`. Independent of #243.
- **What to pull, in order:** `DEVIN_PULL_LIST.md`.
- **Owner's rules:** nothing merges or deploys without the owner saying so; #243 stays draft until the owner approves it for Devin.

## Open items (owner asked for, not done)
1. Nothing else on the product page is pending; the owner last approved the gallery/alignment pass and the share row.
2. Translations for the new `_()` strings on home/listing (English falls through today).
3. Checkout, profile, success and lens pages still carry their inline CSS (tokens/footer only).
4. Production link previews (WhatsApp/Facebook) stay poor until #243 deploys: the live product page's `og:url` is the site root and the live homepage is 4.9 MB. After deploy, test previews with a never-shared product URL (WhatsApp caches per URL for days).

## How to run it locally anywhere
1. Clone, `ln -s <clone> flaskr` in the parent dir (the package imports as `flaskr`), Python 3.11 venv, `pip install -r requirements-test.txt` plus Flask-MySQLdb Flask-Session Flask-Mail Flask-Compress redis pyOpenSSL.
2. MariaDB with the `optiwar2` schema/catalogue dump (ask the owner), user `oslb6`.
3. `local_app.example.py` beside the clone serves optiwar.in behaviour at https://in.localhost:8010 (adhoc cert); `run_local.example.sh` starts it. Restart after template edits (debug off, templates cached).
4. Product photos: catalog masters are not in git. `local_derivatives.py` builds the trimmed AVIF/WebP/JPEG ladder from masters under `static/catalog/opticalbazaar/<CODE>/`; the launcher hides manifest entries whose ladder is missing so `<picture>` sources never 404 locally.
5. Tests: `PYTHONPATH=<parent of flaskr symlink> python -m unittest discover -s tests -t .` from the clone.
6. Screenshots: headless `google-chrome --headless=new --ignore-certificate-errors --window-size=390,2600 --screenshot=...`. Lighthouse: `CHROME_PATH=... npx lighthouse@12 <url> --chrome-flags="--headless=new --ignore-certificate-errors"`.

## Conventions (from the UI team notes, Oct 2026)
Per-page CSS under `static/css/` with `?v=` bumps; new files go into `DEPLOY_SET` in `deploy/deploy.py` in the same PR; no third-party scripts except Razorpay; keep `_()` on customer strings and escape `%` as `%%` inside them; Lighthouse baselines in `docs/lighthouse/`; screenshots in the PR; every Devin Review finding answered before merge; do not edit `templates/ai_faq.html` or `contact_us.html` until production drift is committed. Keep the product-card hooks (`.heart`, `data-*` on `li`, `.scrolling-track`, `.ow-lens-btn`) intact: favourites, zoom, filters and infinite scroll select on them. `test_lens_order` asserts the `lens_types_header` block in `base.html` ends with `</nav>` and that `<div class="pdp-cta">` exists.

## Other threads the owner is running (not code)
- Redington India partner onboarding (Apple Watch): stuck at CIN verification on onboarding.redingtononline.com; owner said to wait until after Diwali (not before 11 Nov) before contacting Redington's Live Help chat. Gmail draft to the LensBazaar team and KET finance-app draft #9 exist.
- Supplier target list for Ray-Ban Meta / Apple Watch / smart rings lives outside this repo (sourcing project).
