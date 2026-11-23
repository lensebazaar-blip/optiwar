# Trademark clean-up

## Scope

`brand_compliance.py` is the source of truth for the 110 held products (101
`frame_mark`, 9 `lens_sticker`) and all 32 approved replacement names. It keeps
the product ID, SKU, brand and hold kind together so the list can be reviewed
or released one row at a time. IDs 1 (`BB44`) and 362 (`AK98`) are explicitly
kept on sale; `KEEP_ON_SALE` is documentation only.

The held products are removed from both storefronts and customer-facing
catalogues independently of database flags. The apply script sets the three
storefront/listing flags to zero, without changing `product_status`. The 32
renames use the replacement name and its slug before and after the database
script runs. Historical order descriptions are not rewritten.

## Run order

1. Deploy the reviewed code.
2. Run `python3 scripts/brand_compliance_apply.py` and review the dry-run output.
3. Run `python3 scripts/brand_compliance_apply.py --apply --quarantine`.
4. Purge the storefront/cache layers.
5. Submit the updated sitemap in Google Search Console (GSC).

The immediate hold statement generated from `HELD` is:

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

The app's static directory is served directly by nginx. Keep quarantined files
outside it; the script defaults to `/root/brand_quarantine/<timestamp>/` and
preserves each `product_image` relative path. Missing source files are
reported, not fatal. The restore record is written before the database
transaction commits and includes previous column values, completed moves and
missing files. The script never deletes files.

## Customer-visible style labels

The `browline` category combines `product_category_browline` with the legacy
`product_category_clubmaster` flag. The `square` category combines
`product_category_square` with `product_category_wayfarer`. Legacy route and
filter inputs remain accepted as aliases, but customer-facing labels are
Browline and Classic Square. `static/seo/sitemap.xml` is retained because
`models.py` serves it at runtime; the served XML removes held product URLs and
uses renamed slugs.
