# Face-scan WhatsApp template — what to submit in MSG91

Outbound WhatsApp in this application is **template-only** (`notifications.send_whatsapp`
sends `content_type: template`). Free text cannot be sent, so the message that carries the
upload link has to be an approved template with variables. This is the only part of the
face-scan journey that cannot be completed in code: submit it in the MSG91 dashboard,
Meta approves it, and then set `FACE_SCAN_WA_TEMPLATE` to its name.

Until it is set, `/admin/face-scan/new` still creates the link and shows it to staff —
the journey works, the automatic message does not.

## Template to submit

- **Name:** `face_scan_request`
- **Category:** Utility
- **Language:** English (`en`)
- **Header:** none
- **Body:**

```
Hello {{1}}, this is Optiwar. To size your spectacles correctly we need one photo of your
face. Please open this secure link and follow the two steps shown: {{2}}

Hold a bank or credit card flat under your eyes in the photo — it is what lets us measure
in millimetres. The link works once and expires in 72 hours.
```

- **Footer:** `Your photo is used only for your measurements and is deleted afterwards.`
- **Variables:** `{{1}}` customer name, `{{2}}` the upload link

If a button is preferred over an in-body link, submit a **Call-to-action → Visit website**
button of type *Dynamic* with `https://optiwar.in/face-scan/{{1}}` as the URL and move the
name to body `{{1}}`; the component mapping in `face_scan.send_request_whatsapp` then needs
the token, not the whole link, in that variable. The in-body version above is what the code
sends today.

## Sample values for the approval submission

- `{{1}}` = `Anita`
- `{{2}}` = `https://optiwar.in/face-scan/AK55-zzqeyY8eJjnPpqrbYiGCugHMU96ePTUB9ntzSg`

## Configuration once approved

| Variable | Meaning | Default |
| --- | --- | --- |
| `FACE_SCAN_WA_TEMPLATE` | approved template name; empty disables sending | *(empty)* |
| `FACE_SCAN_LINK_BASE` | absolute base for the link, e.g. `https://optiwar.in` | request host |
| `FACE_SCAN_TOKEN_HOURS` | link lifetime | `72` |
| `FACE_SCAN_RETENTION_DAYS` | how long the photo is kept before `purge_due_images` deletes it | `90` |

`FACE_SCAN_LINK_BASE` should be set explicitly in production: the link travels over
WhatsApp, so it must be absolute and must point at the storefront the customer expects
(`.in` or `.com`), not at whichever host happened to serve the admin request.

## A customer who was already asked — where the photo comes from

There are only three places a face photo can reach us, and only one of them is automatic:

1. **The customer used their link** → it is already on `/admin/face-scan`, with the
   measurement form beside it. `image_source = customer_link`.
2. **The customer replied on WhatsApp with the photo** → we cannot fetch it. MSG91 hands over
   inbound media only through its *On Message Received* webhook, which this application does
   not implement (and which is paused). The photo is in the WhatsApp Business inbox on the
   provider side; save it from there and file it with **Photo already received?** on
   `/admin/face-scan`, or *Attach photo* on an existing request. `image_source = staff_upload`.
3. **The customer emailed it** → same as 2: save the attachment and file it.

A staff-filed photo takes the same validation, audit, retention and measurement path as a
link upload. The one difference is consent: the customer did not tick the box, so the staff
member asserts it and `received_by` records who did.

Filing a photo consumes the customer's link for that request, so a photo staff have already
measured cannot later be silently replaced by a late upload.

## Retention

`face_scan.purge_due_images(db)` deletes photos whose `purge_after` has passed and keeps
the measurements a human read from them. It is a plain function so it can be run from cron
or from the existing closure job; **retention is not automatic until something calls it.**
