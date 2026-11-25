# Trademark clean-up

## Scope

`brand_compliance.py` is the source of truth for the 110 trademark-held products,
the 20 released products, the 19 released products still pending photo approval,
and all 131 approved replacement names. It keeps the product ID, SKU, brand
and hold kind together so the list can be reviewed or released one row at a
time. IDs 1 (`BB44`) and 362 (`AK98`) are explicitly kept on sale;
`KEEP_ON_SALE` is documentation only.

Blocked products are removed from both storefronts and customer-facing
catalogues independently of database flags. `HELD` remains the trademark-only
list; the effective blocked set also includes `PHOTO_PENDING`. The apply script
sets blocked products' storefront/listing flags to zero, without changing
`product_status`. The 131 renames use the replacement name and its slug before
and after the database script runs. Historical order descriptions are not
rewritten.

## Round 2

| Action | PID | SKU | Details |
| --- | ---: | --- | --- |
| Hold | 93 | AH69 | Tommy Hilfiger (lookalike flag) |
| Hold | 961 | AB87 | Royal Navy; quarantine both `opticalbazaar/AB14/AB87/` and `opticalbazaar/AB87/` image families |
| Hold | 482 | AA05 | Oppo/Vivo (oppvivo) |
| Rename | 150 | AM85 | OPTIWAR Rimless; formerly DASAULTS |
| Rename | 154 | AM84 | OPTIWAR Rimless; formerly DASAULTS |
| Remove photos | 150 | AM85 | `AM85_2.jpg`, `AM85_4.jpg` |
| Remove photos | 154 | AM84 | `AM84_1.jpg`, `AM84_2.jpg`, `AM84_4.jpg` |

1. Review the dry run and all summary counts before applying:
   off-sale, relisted, renamed, SEO-scrubbed, photos to quarantine and photos
   to republish, plus guarded stock updates. The dry run lists each planned
   quarantine, photo removal and copy. It uses
   the round-one restore record
   `/root/brand_compliance_restore_20261123-163137.json` to find quarantined
   source photos, with a quarantine-directory fallback.
2. After approval, run `--apply --quarantine` with the live static image root.
   Public photos for blocked products are moved outside `static/`. Relisted
   products are copied from quarantine to `static/catalog/r2/<SKU>/` under new
   filenames, and `products.product_image` is updated to those new paths.
   `PHOTO_REMOVE` filenames are removed from `products.product_image` and
   quarantined individually. `STOCK_SET` changes `BQ49` (PID 225) from quantity
   1 to 0 for WMRA-1539 only if the current quantity is still 1; it does not
   change order status or notify customers. Old slugs for renamed sold-out
   frames still redirect to the replacement slug. The old catalog image paths
   remain quarantined. The restore JSON records the DB values, moves and copies.
3. The current release is AB85 (PID 696). The other 5 released frames remain
   blocked by `PHOTO_PENDING`. **The remaining 5 relist by deleting them from
   `PHOTO_PENDING` after photo approval.** Do not delete them from `HELD` again;
   they are already removed from the trademark list.
4. Rebuild the SEO product files and purge page/image caches only after a
   successful apply. Keep `static/seo/sitemap.xml`; the app serves it at
   runtime and removes blocked product URLs while updating renamed slugs.

## Run order

0. If an immediate hold is required before deployment, optionally run the
   literal SQL below.
1. Deploy the reviewed code.
2. Run the dry run below and review its database and image paths.
3. Run `--apply --quarantine` with the live static image root.
4. On the server, delete only `static/seo/products/` and
   `static/seo/products.jsonld`, then purge page and image caches.
   **Keep `static/seo/sitemap.xml`**: the live file contains 688 URLs and is
   served by the app. Without it, `/sitemap.xml` falls back to a single URL.
5. In Google Search Console (GSC) for both `optiwar.com` and `optiwar.in`,
   resubmit the sitemaps and request removal of held product and image URLs.

## Server commands

`deploy/deploy.py` installs the application into
`/var/www/flask-optiwar-ow-release-090525/venv/lib/python3.11/site-packages/flaskr`.
Both `brand_compliance.py` and `scripts/brand_compliance_apply.py` are in its
deployment set; run the script from that application root so its imports and
relative paths resolve to the deployed code.

The script reads the same `MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD` and
`MYSQL_DB` environment values as `scripts/restock_refunded.py`. Run these
commands in the authorized server shell with those values supplied through the
existing secure Ops environment; do not put credentials in command arguments
or this repository. The interactive shell does not inherit systemd variables:
use the established secure procedure to export the same values used by
`gunicorn`'s `EnvironmentFile`. Its path can be identified with
`systemctl show gunicorn -p EnvironmentFiles --value`.

```bash
set -e
APP_ROOT=/var/www/flask-optiwar-ow-release-090525/venv/lib/python3.11/site-packages/flaskr
PYTHON=/var/www/flask-optiwar-ow-release-090525/venv/bin/python
cd "$APP_ROOT"

"$PYTHON" scripts/brand_compliance_apply.py \
  --image-root "$APP_ROOT/static"

"$PYTHON" scripts/brand_compliance_apply.py \
  --apply --quarantine --image-root "$APP_ROOT/static"

rm -rf -- "$APP_ROOT/static/seo/products" \
  "$APP_ROOT/static/seo/products.jsonld"
```

Run the deletion only after the apply succeeds. It removes only the obsolete
per-product SEO files; do not remove `static/seo/sitemap.xml`.

To restore a recorded run, use its restore file with the same image root:

```bash
RESTORE_FILE='/root/brand_compliance_restore_<timestamp>.json'
"$PYTHON" scripts/brand_compliance_apply.py \
  --restore "$RESTORE_FILE" \
  --image-root "$APP_ROOT/static"
```

The following immediate-hold statement is historical Round 1 SQL only; do not
reuse it for Round 2. The current effective block list is `blocked_ids()`.

```sql
UPDATE products SET sell_on_in=0, sell_on_com=0, show_in_listings=0 WHERE product_id IN (26,27,29,45,47,49,94,96,97,111,112,136,137,141,145,146,170,178,203,204,214,218,219,220,222,223,224,225,261,298,302,330,331,332,333,334,336,340,366,370,372,373,374,411,423,424,426,427,428,429,430,433,434,436,437,441,442,444,464,485,524,525,528,530,531,532,533,534,545,547,549,550,551,552,584,587,660,663,665,666,668,669,672,675,684,686,689,690,691,692,693,694,695,696,767,768,770,776,808,813,814,824,829,842,845,860,865,934,935,965);
```

## Releasing a frame

Release only after the image and listing name have passed the six checks below
and the products team approves it. Remove that product's row from `HELD` and
deploy the change. For a frame already quarantined, use its `image_moves` entry
in the restore record to return only that frame's files, then set its
`sell_on_in`, `sell_on_com` and `show_in_listings` flags to the approved
values. Do not change `product_status`. The script's `--restore` reverts the
entire recorded batch; use it only when the whole batch is being rolled back.
For the 20 Round 2 releases, follow the `RELEASED`/`PHOTO_PENDING` procedure
above instead of this historical row-by-row release process.

## Rules for every new listing

A frame goes live only when all six checks pass. If one fails, keep the frame
off the site for the products team to decide.

1. **Frame:** no other company's name, logo, monogram or lookalike mark on the
   arms, hinges, front, nose pads or tips, inside or out.
2. **Lens:** remove the demo-lens sticker before photographing. Never list a
   frame whose photo shows any sticker but ours.
3. **Photos:** take our own. Use a vendor or catalogue photo only after someone
   has checked every image of that frame at full size.
4. **Name:** use `OPTIWAR + shape + SKU`. No brand, celebrity, film, car, hotel
   or famous-place names, and no near-spellings of them.
5. **Style words:** use only Rectangle, Square, Classic Square, Round, Oval,
   Browline, Aviator, Cat Eye, Panto, Half-Rim (Supra), Rimless and Kids.
   Never use Clubmaster or Wayfarer.
6. **Description:** no “Made in Italy”, “Italy design” or similar claim unless
   we hold proof of origin.

Only a frame bought from the brand or its authorised distributor may carry a
brand. List it under that brand, never under Optiwar.

## Image quarantine

The app's static directory is served directly by nginx. Quarantine every
blocked product's photos, including `lens_sticker` products whose photos show
third-party stickers. Keep quarantined files outside the public static root;
the script defaults to `/root/brand_quarantine/<timestamp>/` and preserves
each `product_image` relative path. `--image-root` defaults to the repository's
`static/` directory and must be set to the live app's `flaskr/static/` directory
when running on the server. Missing source files are reported, not fatal. The
restore record is written before the database transaction commits and includes
previous column values, completed moves, copied files and missing files.
`--restore` returns quarantined files and removes only copies created by that
recorded run.

## Customer-visible style labels

The `browline` category combines `product_category_browline` with the legacy
`product_category_clubmaster` flag. The `square` category combines
`product_category_square` with `product_category_wayfarer`. Legacy route and
filter inputs remain accepted as aliases, but customer-facing labels are
Browline and Classic Square. `static/seo/sitemap.xml` is retained because
`models.py` serves it at runtime; the served XML removes held product URLs and
uses renamed slugs.
