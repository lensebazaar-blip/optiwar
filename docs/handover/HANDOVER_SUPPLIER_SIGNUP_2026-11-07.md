# Become a supplier (7 Nov 2026, PR #244)

`supplier_signup.py` (+ `templates/supplier_signup.html`, shared `static/css/ow-forms.css`), registered on the main
blueprint: `GET/POST /become-a-supplier`. Footer link "Become a supplier". Both storefronts.

* **Fields**: business name and type (manufacturer / wholesaler / distributor / importer / brand owner), GSTIN,
  website, years in business; registered address, city, state (GST location), pin, factory / warehouse address;
  product categories (frames, sunglasses, lenses, contact lenses, smart eyewear, accessories, other), brands,
  monthly capacity / MOQ; contact person, designation, email, phone; message; consent. Honeypot; 3 per session.
* **GSTIN is validated**: 15 characters, state code from the GST table, PAN shape, 'Z', and the checksum character
  (`gstin_checksum`). The GST state is read from the number and must match the state the supplier selected; the PAN
  is derived and stored. Example that passes: 06AAHCK4567A1ZP (Haryana).
* **Reference** `SUP-YYYYMMDD-XXXX`; review promised within 7 working days (Sundays skipped).
* **Stored first** in `supplier_applications` (additive table; `status` column NEW for an admin queue later), then
  **two mails** in one SMTP session (`mail.send_supplier_signup`): the application to admin@optiwar.com and
  admin@lensbazaar.com (Reply-To the supplier) and the **signup confirmation letter** to the supplier (Cc the admins):
  reference, review date, documents to keep ready (GST certificate, PAN, cancelled cheque, catalogue with wholesale
  prices, samples), and that Optiwar buys its own inventory and is not a marketplace. Recipients overridable with
  `SUPPLIER_SIGNUP_RECIPIENTS`. The page shows the letter and a Print button whether or not the mail went.
* Ops: `SELECT * FROM supplier_applications WHERE status='NEW' ORDER BY created_at` is the queue until an admin page
  exists; update `status` by hand (NEW / CONTACTED / ONBOARDED / DECLINED).

Tests: `tests/test_supplier_signup.py` (11): GSTIN shape, state code, checksum, state match; required fields; reference,
text and letter; storage never raising; the route with DB and mail faked; wiring (models, footer, deploy lists,
migration, shared CSS). Full suite 1797 OK.
