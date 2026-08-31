# Contact lenses: schema, and how data gets in

What exists today, where it deliberately differs from the instruction it was
written against, and the contract the importer must satisfy before ~70 products
are written. Read the schema itself in `contact_lens.py`; this document exists
for the decisions the DDL cannot state.

## 1. Why an extension rather than the frame columns

`products` keeps its meaning as the commercial record — price, slug, images,
status, cart identity, site eligibility — so a lens is bought, priced and
rendered by the code that already works. Everything that is true of a lens and
meaningless for a frame lives beside it:

```
products                    commercial: price, slug, image, status, sell_on_*
contact_lens_products       one row per lens: brand, modality, pack, availability
contact_lens_variants       one row per orderable parameter combination
contact_lens_images         per-colour imagery
```

A prescription combination is **not** a `products` row. 30 sphere powers × 4
cylinders × 4 axes is one product and ~480 variants, not 480 products: it must
not multiply the catalogue, the sitemap, the feed or the AI's search space.

`cl_range_model.py` (hardcoded `-10..0` ranges per `product_id`) is superseded.
The variant table is the authority, and it is not migrated from those ranges —
a range is not a matrix (see §5).

## 2. Reconciliation with the requested column list

Four deliberate divergences. Each is a decision, not an omission.

| Requested | Actual | Why |
|---|---|---|
| `contact_lens_products.sell_on_com` / `sell_on_in` | `products.sell_on_com` / `sell_on_in` | Eligibility is decided once, in `catalogue.py`, for every vertical and every surface. A second copy on the lens table would be a second answer, and the whole invariant is that `.in` cannot see a lens by *any* route. |
| `replacement_schedule` | `modality` (`DAILY`/`MONTHLY`/`CONVENTIONAL`) + `replacement_days` | The word is what a shopper reads and the number is what a reorder reminder computes. Both are stated by the manufacturer; neither is derived from the other. |
| `base_curve`, `diameter` on the product | on `contact_lens_variants` | Several lenses are sold in more than one base curve, which makes BC an orderable parameter, not a product fact. A single-BC lens has the same value on every variant row, and the product page reads it from there. |
| `pack_size` | `pack_quantity` | Naming only. It is lenses per box, and per §6 the price is per box. |

`merchant_enabled` is on `contact_lens_products`, defaulting to `0` — a lens is
in the database and on no surface until somebody asserts readiness.

Where a lens came from is carried by three columns and a unique key, added with
the importer because that is what they are for (§4):

```
contact_lens_products.source_system   VARCHAR(32) NULL   -- 'lensbazaar'
contact_lens_products.source_ref      VARCHAR(64) NULL   -- their product id/SKU
contact_lens_products.imported_at     DATETIME NULL
UNIQUE KEY uq_cl_source (source_system, source_ref)
```

Idempotence is the index's job rather than the importer's memory: two rows
claiming the same source product are refused by the database, not by a check
somebody can forget to run.

## 3. Availability is not frame stock

A lens is continuously replenished, so an order does not decrement
`product_quantity` and a lens is **never** `OUT_OF_STOCK`:

```
IN_STOCK   ships from stock
ON_ORDER   purchasable, with lead_time_days / expected_available_at shown
```

Anything that maps `product_quantity = 0` to unavailable must exclude
`product_vertical = CONTACT_LENS`. The feed, the product page and the cart read
`contact_lens_products.availability`, not the quantity.

## 4. The importer contract

```
LensBazaar export (xlsx)
        ↓  read-only, never written back to
staging (per-run table or temp schema)
        ↓  validate every row, resolve nothing by guessing
dry-run report  ← default, and the only output until --apply
        ↓
products + contact_lens_products + contact_lens_variants + images
```

Non-negotiable properties:

- **Dry run is the default.** `--apply` and `--by` are typed by a person;
  without them the run only reports. `--only` imports named `source_ref`s, which
  is how the four-product pilot goes in ahead of the rest.
- **Idempotent by `(source_system, source_ref)`.** Re-running the same export
  upserts; it does not create a second product. Variants upsert on
  `(product_id, variant_sig)`, which is why `variant_sig` exists — MySQL treats
  NULLs as distinct, so a plain `UNIQUE (sph, cyl, axis, ...)` would admit ten
  copies of `-2.00, NULL, NULL`.
- **No DELETE.** A variant withdrawn by the manufacturer is `available = 0`, so
  an order that referenced it remains explicable.
- **One transaction per product**, product and its variants together. A product
  whose matrix fails validation is rolled back whole and reported; the rest of
  the run continues.
- **Only lens-owned fields.** The importer never touches a frame, a status, an
  order, or any `products` column outside the set it declares.
- **Rejections, not repairs**: a malformed matrix row, a duplicate GTIN across
  two products, a `TORIC` lens with no cylinder, a cylinder with no axis, an
  axis outside 0–180, a price that is not EUR, a missing image or landing page.
- **Audit**: `imported_at`, `source_system`, `source_ref` per product, and the
  run's report kept.

Implemented as `cl_import.py` (parse, validate, refuse — no database and no
flask, so a rejection is provable in a test) and
`scripts/import_contact_lenses.py` (the writing, one transaction per product).
Rules the validator enforces, each because the alternative is selling a lens
that does not exist or is not the one prescribed:

| Refused | Why |
| --- | --- |
| no GTIN **and** no MPN | the offer would have to claim our `product_code` as the manufacturer's |
| `OUT_OF_STOCK` | not a state a replenished lens has (§3) |
| `ON_ORDER` with no lead time | a customer told to wait has to be told how long |
| a power off the quarter-dioptre step | a transcription error in the export, not an exotic lens |
| plus-form cylinder | manufacturers state minus cylinder; a transposed sign is a different lens |
| `TORIC` with no axis, `SPHERICAL` with a cylinder, `MULTIFOCAL` with no add | the row landed in the wrong column, or the lens is not orderable |
| axis outside 0–180 | not a position on the dial |
| a GTIN two products claim | an identifier two products claim identifies neither |
| a duplicate combination | reported against its spreadsheet row, rather than surfacing as a key violation mid-import |
| a product with no available combination | nothing to sell |

A product with any rejected row is held back **whole**, because half a matrix
would sell the half that loaded. Other products in the same export still import.

Order of work: the four pilot products (`--only`), production acceptance, then
the rest.

## 5. What the importer will not do

Manufacture combinations from a range. `MyDay Toric` published as
`SPH plano..-10.00 / +6.00` and `CYL -0.75/-1.25/-1.75/-2.25` does not define a
matrix: sphere steps change across the range, axis availability differs by
cylinder, and some CYL/SPH pairs are not made. The cross product of the stated
minima and maxima would be a catalogue of powers a customer can order and nobody
can supply.

So the export must carry the actual valid combinations, as rows. Where it does
not, that product is not imported. The same rule applies to GTIN, MPN, material,
water content, BC, DIA and imagery: absent is absent, and no value is inferred
from model knowledge or from a similar lens.

## 6. Pricing and the cart

Price is **per box**, in EUR, and quantity is boxes per eye:

```
RIGHT  MyDay Toric  -4.50 / -1.25 x 180   × 2 boxes
LEFT   MyDay Toric  -4.00 / -0.75 x 170   × 3 boxes
                                      charge = box price × 5
```

Left and right are independent: different prescription, different quantity, and
each must match a variant row with `available = 1` at add-to-cart, at checkout
and again at order creation. A near match is refused, never rounded.

## 7. Surfaces, and the single flag

A lens is live on a surface when `live_lenses()` says so — `sell_on_com = 1`,
`merchant_enabled = 1`, a landing page, a primary image, an EUR price, an
availability, a brand, a GTIN or MPN, and at least one available variant. Every
surface reads that one function:

```
product page · category · search · cart · API · AI catalogue
SEO/canonical · JSON-LD · sitemap · GMC feed
```

`.in` is `sell_on_in = 0` for every lens, and the invariant is that no route —
direct URL, search, API, AI, sitemap, structured data, feed — returns one. It is
monitored daily as `contact lenses exposed on .in: 0`, red if ever above zero.

India activation is then a change of that flag plus regeneration, not another
application build.

## 8. GMC mapping

From the canonical lens record, never from the frame feed's assumptions:

```
brand         contact_lens_products.brand        (Alcon, CooperVision, ...)
mpn           manufacturer_mpn                   never product_code
gtin          gtin, when the manufacturer states one
id            product_code                       our internal offer id only
```

Plus pack quantity, modality, lens type, material, BC/DIA where applicable,
availability, an accurate title and description, and the correct image.
`brand = Optiwar` with `mpn = product_code` on another manufacturer's lens is a
misrepresentation, and the mapping must make it impossible rather than unlikely.
