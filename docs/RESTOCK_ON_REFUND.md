# Restock on refund

A full refund restores stock only when the order has not shipped. Each fulfilled
order line is restored once, guarded by the `stock_restocks.order_line_id`
primary key. Pending or refund-pending lines and `SMART_EYEWEAR` products are
excluded. Test orders, orders with any `Shipped`, `Complete`, `Delivery-assist`,
`Returned`, `Processed-Reverse` or `Shipped-Reverse` status, and orders with an
`ops_shipping_awb` row are skipped. Restocking reactivates only an
`OUT_OF_STOCK` product; deliberate `SEASONAL`, `DISCONTINUED` and `ARCHIVED`
statuses are preserved. Partial refunds never restore stock.

The refund path records the refund outcome first, then commits stock restoration
separately. A restock error is logged and does not undo or fail the refund.

## Backfill

The script defaults to a dry run and selects full refunds in `PROCESSED` or
`PENDING` status requested within the last 90 days. Set the same
`MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD` and `MYSQL_DB` environment values
used by the application.

```bash
python3 scripts/restock_refunded.py --by <your-name>
python3 scripts/restock_refunded.py --by <your-name> --days 180
python3 scripts/restock_refunded.py --by <your-name> --order ORDER-ID
python3 scripts/restock_refunded.py --by <your-name> --apply
```

Repeat `--order` to select multiple orders. `--apply` commits one order at a
time; rerunning is safe because each order line can be recorded only once.
