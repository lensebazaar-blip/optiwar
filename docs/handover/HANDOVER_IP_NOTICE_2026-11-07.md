# Trademark / copyright holder notice form (7 Nov 2026, PR #244)

`ip_notice.py` (+ `templates/ip_notice.html`), registered on the main blueprint: `GET/POST /ip-notice`.
Linked from the Terms (section "Trademark and Copyright Holder Notices") and the footer ("Brand & copyright notices").
The old `/contact_us` link in the Terms pointed at a retired form that redirects to the assistant; it is gone.

* **Fields** mirror the Terms' list: holder name, designation, company, phone, email, postal address, right type,
  registration / proof, trademark or work, listing URL(s), reason, action requested (+ detail), the good-faith
  declaration and the hold-period confirmation. Honeypot field; 3 notices per session (429 after).
* **Reference** `IPN-YYYYMMDD-XXXX`; acknowledgement due 7 working days (Sundays skipped); hold period 90 days
  from the acknowledgement (both dates are shown and mailed).
* **Stored first** in `ip_notices` (additive `CREATE TABLE IF NOT EXISTS`, in `deploy.py migration` via
  `ip_notice_module().TABLES`); mail is best effort (`mail_sent` flag), the acknowledgement page always shows the
  reference and the full notice text (Print / save as PDF, "Email myself a copy").
* **Mail** (`mail.send_ip_notice`): one SMTP session, two messages: the notice to **admin@optiwar.com and
  admin@lensbazaar.com** (Reply-To the rights holder) and a courteous acknowledgement to the rights holder
  (Cc both admins) that asks them to hold any further action until our written reply, 90 days at most and
  usually sooner. Recipients can be overridden with `IP_NOTICE_RECIPIENTS` in app config.
* **Assistant** (`chat_gateway`): `ip_notice.prompt_section()` is appended to every system prompt: for a brand /
  copyright complaint be courteous, do not argue, point to `/ip-notice` or the postal address, explain the
  7-working-day acknowledgement and the up-to-90-day hold, ask them to hold action; never collect the complaint
  in chat; quote only admin@optiwar.com.
* **Wording**: Optiwar is described as inventory-led and direct to consumer, not a marketplace (Terms and texts).

Tests: `tests/test_ip_notice.py` (12): parsing, dates, reference, texts, storage never raising, the route with
DB and mail faked (stored, two-party mail, 400 on incomplete, honeypot and flood refused), wiring. Full suite OK.
