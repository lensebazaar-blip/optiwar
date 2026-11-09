# Eyeglasses prescription page + photo reader (7 Nov 2026)

Part of PR #244 (`ui/profile-and-account`). Nothing deployed; the flag-free parts go live with the PR.

## The page (`templates/prescription.html`, `static/css/rx-page.css`)
Reached from a frame's **Buy with Prescription Lenses** (POST `/add_prescription`). Rebuilt mobile-first:
Back-to-frame link and a Frame → Lenses → Cart progress line; the frame strip with the price; step 1 the
prescription (scan door, then SPH/CYL/AXIS as a small table, ADD on demand); step 2 the lens options
(same modals); step 3 the summary. Phones get one fixed bar (total + Add to cart) above the tab bar;
from 1024 px the summary sits in a sticky right column; 1440/1920 widen the grid.
Every form hook is unchanged: select ids/names, hidden inputs, modal ids, radio names and `data-price`,
`#checkoutButton`, `#lensRecommendation`, the pricing script and `/api/lens-pricing` context switching.
One old bug fixed: a power restored from localStorage as `"0"` never matched the `"0.00"` option, so the
PWR select came back blank on a second visit.

## The reader (`rx_photo.py`, `POST /api/rx-photo/read`, field `rx_photo`)
* **Eyeglasses only.** Its own prompt (SPH/CYL/AXIS/ADD/PD, no base curve). The answer is snapped to the
  page's own dropdown steps (0.25; 0.50 below −6.00 SPH / beyond ±6.00 CYL; axis to 5°) and a value
  outside the range sold is left empty with a note, never clamped or invented.
* **Provider = the existing setting.** `LENS_RX_VISION_PROVIDER` (`openai_vision` default,
  `deepseek_vision` with `DEEPSEEK_VISION_MODEL`, default `deepseek-vl2`) through `ai_client.call_model`,
  the same pools, deadlines and metrics as the contact-lens upload and the chat photo reader.
* **Nothing stored, no sign-in.** The photo is normalised in memory (`lens_documents.normalise`: JPEG/PNG/
  WebP/HEIC/PDF, 12 MB cap, EXIF orientation, ≤2000 px) and dropped. Reads are capped at 8 per session
  (429 `RX_PHOTO_LIMIT`). Refusals: `RX_PHOTO_REJECTED` 400, `RX_PHOTO_UNREADABLE` 422,
  `RX_PHOTO_PROVIDER_FAILED` 503; each is a `dev_defects` record.
* The page shows "Read from your photo. Check every value…" with the notes; filled selects glow green
  for a few seconds; an ADD in the reading opens the ADD row exactly as typing does (which also
  pre-selects the Flat-top D bifocal, the existing behaviour).

## Ops / config
No schema change, no new environment variable. Production already has the vision provider configured for
`/contact-lenses/rx-upload`; the same key serves this. To switch to DeepSeek: `LENS_RX_VISION_PROVIDER=deepseek_vision`.
`DEPLOY_SET` += `rx_photo.py`, `static/css/rx-page.css`, `templates/prescription.html`;
`NEW_IN_RELEASE` += `rx_photo.py`, `static/css/rx-page.css` (and `static/css/ow-ui.css`, also fixed on #243).

## Tests
`tests/test_rx_photo.py` (16): snapping, option values, parsing, the route with the model faked (visitor OK,
provider switch, refused file never reaches the provider, 503/422/429), wiring (template ids, registration,
deploy lists). Full suite: 1761 run, OK, 380 skipped (no local DB).

## Screenshots
`docs/screenshots/prescription/`: mobile before/after, tablet, desktop 1440 and 1920, and the read-from-photo
state on mobile and desktop.
