# Writing off lost stock

Use this when physical stock no longer exists — a box lost in transit, damaged,
stolen or unaccounted for at stock-take. It removes the units from sale and
records who wrote them off and why, so the zero is explainable later.

Do not use it to cancel an order, refund a customer or hide a sale. It never
touches `orders`, payments or `sales_log`.

## Where inventory actually lives

`products.product_quantity` in the Optiwar database is the single source of
truth for frame stock. The storefront, `/api/products`, the AI recommender and
the Google Merchant feed all read it.

`https://eu.lensbazaar.com/ops/` is a separate dashboard. Its only write path
into this system is lens **pricing** (`/api/lens-pricing/update`, shared-secret
authenticated — see `pricing.py`). There is no inventory endpoint, so that
dashboard cannot remove frame stock and does not need to: once the write-off
runs here, its product views follow within the 300s cache TTL.

If the order-processing team keeps its own copy of stock counts, they must
re-read `/api/products` after a write-off rather than be told counts by hand.

## Running it

Dry run first — this is the default, and it prints every row it would touch:

```bash
python3 scripts/stock_writeoff.py --box 205 \
  --reason "box lost in transit" --by <your-name>
```

Then apply:

```bash
python3 scripts/stock_writeoff.py --box 205 \
  --reason "box lost in transit" --by <your-name> --apply
```

Select by product instead of by box with `--ids 784,785,790`.

`--status DISCONTINUED` instead of the default `OUT_OF_STOCK` if the frames will
never be restocked. `OUT_OF_STOCK` keeps the product page live and lets stock
return; `DISCONTINUED` removes it from browsing for good.

## What it refuses to do

It **stops** if a customer has *paid* for one of those frames and it has not
shipped — a successful `payment_collector` row on an unfulfilled, non-test,
non-archived line — and names the lines. Money taken for stock that no longer
exists is a fulfilment decision, not an inventory one: refund, cancel or source
a replacement first. `--force` records the write-off anyway and is only correct
once those lines have been dealt with.

Unpaid open lines are listed but do **not** block. `fulfillment_status='pending'`
is also where an abandoned cart comes to rest — there are 383 of those, the
oldest from 2024 — so treating them as customers waiting would make the tool
impossible to run for honest reasons.

It leaves a product that is **already** empty completely alone — including its
`status_reason`. So a frame that sold out through a real order keeps
`auto: stock depleted` and its original `sold_out_at`, and re-running the tool
changes nothing and adds no history.

It never overrides a deliberate `SEASONAL`, `DISCONTINUED` or `ARCHIVED` state;
the quantity goes to zero but the commercial decision stands.

## Undoing it

Every apply writes `restore_<timestamp>.sql` before changing anything, holding
each product's exact prior quantity and status:

```bash
mysql <db> < restore_20260826-035902.sql
```

That restores what is sellable. It deliberately does not delete the
`product_status_history` rows — the write-off happened, and the audit trail
should say so.
