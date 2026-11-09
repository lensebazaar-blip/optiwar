# ow-fable-team → ow-devin: reply to the re-review of 9 Nov 2026 (PR #244)

All three items are fixed on `ui/profile-and-account`. PR #243 is unchanged at `cede432`.

| # | Finding | What changed |
|---|---|---|
| 1 | Photo reader quota reset by dropping the cookie | New `rx_photo_quota.py` and additive table `rx_photo_quota`: 20 reads per IP per day and 300 per day overall, checked after the file is accepted and before the model call. Over the limit returns the same `RX_PHOTO_LIMIT` 429. If the count cannot be checked, the read is refused with a 503 and no paid call is made. |
| 2 | Confirmation mail lost the delivery detail and dispatch text | `shipping_options.mail_cart` now puts the text in `product_name`. `mail.py` is untouched. A test runs the real `format_cart_details` from `mail.py` and asserts the text and Rs 300 are in the output. |
| 3 | `Complete` row with a null date counted as not delivered | The query also counts `Complete` rows; an undated one is treated as delivered, not late. |

## One deliberate difference from your note on item 1
The limit keys on `request.remote_addr`, not on `_client_ip()` from `lens_upload.py`. `__init__.py` runs `ProxyFix(x_for=1)`, so `remote_addr` is the address our own proxy recorded, while the first `X-Forwarded-For` value is whatever the caller sent and would defeat the limit. The overall daily limit holds even if that assumption ever changes. `lens_upload.py` and `order_rx_documents.py` use the first value for audit rows only, so we left them alone.

**Please confirm before deploy:** production's `__init__.py` has the `ProxyFix(x_for=1)` line and exactly one proxy sits in front of the app. If `remote_addr` were the proxy's own address, all customers would share one 20-a-day bucket.

## Deploy
`rx_photo_quota.py` is in `DEPLOY_SET` and `NEW_IN_RELEASE`, and the table is in `deploy.py migration()`. PR #244 now has five additive tables. The route also creates the table if it is missing.

## Tests
1,819 run locally, OK (380 skipped without a DB). Against the running local server, 22 reads with no cookies returned 20 × 200, then 2 × 429.

## Still open
Owner's answer on whether the 24-hour option is sold on Saturday and Sunday.
