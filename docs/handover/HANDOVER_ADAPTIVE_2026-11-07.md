# Adaptive + navigation pass (7 Nov 2026, PR #244)

Owner's ask: every page fills old and new phones, tablets and large desktops, and each page says where you are.

## What changed (`static/css/ow-ui.css` `?v=18`)
* `--ow-max` replaces the ten hard-coded `1240px` caps (wrap, header, lens bar, listing grid and head, breadcrumb,
  home, flashes, footer). It is 1240 px by default, 1440 px from 1600 px, 1640 px from 1920 px.
* Listing grid: 5 columns from 1600 px, 6 from 1920 px (also the `ul#product-list` override).
* Home hero: two columns from 768 px (was 900), carousel capped at 560 px in the one-column layout so tablets
  no longer get a 560 px-tall card. Larger hero type from 1920 px.
* Product page: the two buy buttons stack between 768 and 959 px instead of wrapping mid-word.
* Checkout from 768 px: the tall "Review and pay." hero is a slim bar (badge, title, sub, Continue shopping);
  the cart column is the widest of the three (1.3 / 1.1 / .9).
* Profile container 1100 px from 1024 px, 1240 px from 1600 px.
* `.ow-pagenav`: back link (history-aware, falls back to the href) + breadcrumb, rendered by the
  `pagenav()` macro in `templates/_pagenav.html`; used on favourites, order lookup (`/my_orders`) and profile.
  Checkout's bar carries a Continue-shopping link. Listing and product pages already had breadcrumbs; the
  prescription page has its own Frame → Lenses → Cart line.

## Deploy
`DEPLOY_SET` and `NEW_IN_RELEASE` += `templates/_pagenav.html`. No schema, env or JS change.

## Screenshots
`docs/screenshots/adaptive/`: before/after at 1920 (home, listing, checkout, profile) and 820 (home, checkout,
favourites, product), plus 390 after-shots to show phones unchanged.
