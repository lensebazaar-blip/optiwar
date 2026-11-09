# 24-hour schedule, Ops controls, photo reader protection (9 Nov 2026, PR #244)

## Owner's answer to your question
The Rs 1000 24-hour option is **not sold on Sunday**. On **Saturday** it is sold only for orders received by **11:45 India time**, and those orders are committed for 24 hours, so their promised date is Sunday. All times use India time whatever the server's zone. The rest of the week is unchanged.

## 24-hour visibility (`shipping_options.py`, `templates/checkout.html`)
* Shown only after a **full 6-digit NCR pin and a state that agrees with it** are entered (110 Delhi; 122 Haryana / Gurgaon; 2010, 2011, 2013 Uttar Pradesh / Ghaziabad / Noida). The checkout has no city field, so the pin stands for the city.
* The option disappears from an open page when the Saturday cutoff passes.
* `POST /checkout` re-checks both, and also the price the customer saw (`shipping_fee_seen`). If the option has closed or the price changed, no order is made and the customer returns to checkout with a message. Nobody is charged a price they did not see.

## Devin: please push these to the Ops team's dashboard
`ops_controls.py` provides the page and the API. Auth is `ops._require_ops_auth` (admin session or Bearer `OPS_API_TOKEN`). Send the person's name in `X-Ops-Operator` when using the token.

| Route | Use |
|---|---|
| `GET /ops/controls` | Ready-made page on the site |
| `GET /api/ops/controls` | Settings, indicators, flagged uploads as JSON |
| `POST /api/ops/controls` | `{"key": K, "delta": 50}` to increase or decrease, or `{"key": K, "value": 1250}` to set |
| `POST /api/ops/controls/flags/<id>/review` | A person has reviewed a flagged upload; also lifts the 24-hour IP block |

| Key | Default | Allowed |
|---|---|---|
| `ncr24_fee` (24-hour price, Rs, 18% GST inclusive) | 1000 | 100 to 10000 |
| `rx_reads_per_ip` (photo reads per IP per day) | 20 | 1 to 500 |
| `rx_reads_per_day` (photo reads per day, all customers) | 300 | 10 to 20000 |

Every change is logged with who and when in `ops_settings_log`. Blink these in the dashboard, using the fields in `indicators`: `day_blink` (80% of the daily limit), `ip_blink` (a customer IP at its limit), `flag_blink` (flagged uploads waiting). A price change never alters orders already placed, because the fee is stored with the order.

## Photo reader protection (`rx_photo.py`, `rx_photo_quota.py`)
**Bots**, checked before any paid call: honeypot field, same-site Origin, a signed page token tied to the session and at least 1.5 seconds old, the site's own image code once per visit for guests (signed-in customers skip it), 4 reads a minute per IP, then the daily limits above.

**Prohibited content:** the vision model also reports a `content_flag` (`child_sexual`, `sexual`, `extremist`, `illegal`, `graphic`). A flagged upload is refused, the IP is blocked from the reader for 24 hours, and `rx_photo_flags` keeps category, file hash, IP and customer id. The image is never stored. The page states the rule beside the Scan button.

**Limits of this:** it is a model's judgement, so it can miss things or wrongly flag. It covers the photo reader only. These upload routes do not have it and they do store files: `order_rx_documents.py` (checkout prescription), `lens_upload.py`, `chat_gateway.py` (chat attachments), `face_scan.py`, `reship_api.py`. Owner to decide on extending it.

## Deploy
New: `rx_photo_quota.py`, `ops_controls.py`, `templates/ops_controls.html` (in `DEPLOY_SET`, `NEW_IN_RELEASE`). PR #244 now has eight additive tables, all in `deploy.py migration()`: the earlier four plus `rx_photo_quota`, `rx_photo_flags`, `ops_settings`, `ops_settings_log`.

On Saturdays Ops must be able to deliver these orders on Sunday, or they will appear under Possible refunds.
