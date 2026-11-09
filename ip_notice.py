"""Trademark / copyright holder notices (Terms, "Trademark and Copyright Holder
Notices" section): the form a Rights Holder fills to tell Optiwar about a
listing, the reference it gets, the dates the hold period runs to, and the
one mail that goes to Optiwar with a copy to the Rights Holder.

Stdlib at import; Flask, the DB and the mailer are imported inside the
functions that need them, so the deploy tool can read ``TABLES`` plainly.

* **Nothing is lost to a mail failure.** The notice is stored first
  (``ip_notices``, additive ``CREATE TABLE IF NOT EXISTS``); the mail is
  best-effort and the acknowledgement page shows the reference either way.
* **The dates are the Terms' dates.** Acknowledgement due 7 working days
  after receipt (Sundays skipped); the 90-day hold period runs from the
  acknowledgement, so "hold until" is the earliest day the Rights Holder may
  proceed, computed from the due date.
* **One mail, both parties.** To the admin notification address, Cc the
  Rights Holder, Reply-To the Rights Holder; the body is the notice in full
  with the reference, so each side holds the same record.
"""
import datetime
import hashlib
import json
import re

PATH = "/ip-notice"
# Where a notice goes (owner, 7 Nov 2026): both admin mailboxes, the Rights Holder acknowledged separately.
RECIPIENTS = ("admin@optiwar.com", "admin@lensbazaar.com")
ACK_WORKING_DAYS = 7
HOLD_DAYS = 90
MAX_TEXT = 4000
MAX_PER_SESSION = 3
SESSION_COUNT_KEY = "ip_notice_count"

RIGHTS_TYPES = (("trademark", "Trademark"), ("copyright", "Copyright"), ("both", "Trademark and copyright"))
ACTIONS = (("correct", "Correct the listing (text or image)"),
           ("remove_image", "Remove an image"),
           ("withdraw", "Withdraw the product from sale"),
           ("other", "Something else (say what below)"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS ip_notices (
    notice_id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    reference VARCHAR(24) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL,
    holder_name VARCHAR(120) NOT NULL,
    company VARCHAR(160) NULL,
    designation VARCHAR(120) NULL,
    email VARCHAR(190) NOT NULL,
    phone VARCHAR(40) NULL,
    address VARCHAR(400) NULL,
    rights_type VARCHAR(16) NOT NULL,
    mark_or_work VARCHAR(400) NOT NULL,
    registration VARCHAR(200) NULL,
    listing_urls TEXT NOT NULL,
    reason TEXT NOT NULL,
    action_requested VARCHAR(24) NOT NULL,
    action_detail TEXT NULL,
    ack_due DATE NOT NULL,
    hold_until DATE NOT NULL,
    mail_sent TINYINT(1) NOT NULL DEFAULT 0,
    ip VARCHAR(64) NULL,
    site VARCHAR(32) NULL,
    KEY ix_ipn_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
TABLES = (("ip_notices", SCHEMA),)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
# A product code as the listing names it: letters / digits / dashes with at least one digit ("AN12", "CE-I-379").
_CODE = re.compile(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9\-]*\d)[A-Za-z0-9][A-Za-z0-9\-]{2,}(?![A-Za-z0-9])")

FIELDS = ("holder_name", "company", "designation", "email", "phone", "address",
          "rights_type", "mark_or_work", "registration", "listing_urls", "reason",
          "action_requested", "action_detail")


def _clean(value, limit):
    return re.sub(r"[ \t]+", " ", (value or "").strip())[:limit]


def parse(form):
    """``(data, errors)``: the notice as typed, cleaned and checked against the
    Terms' list of what a notice must contain. ``errors`` is ``{field: message}``."""
    data = {f: _clean(form.get(f, ""), MAX_TEXT if f in ("listing_urls", "reason", "action_detail", "address") else 400)
            for f in FIELDS}
    data["declaration"] = str(form.get("declaration", "")).lower() in ("1", "on", "true", "yes")
    data["hold_agreed"] = str(form.get("hold_agreed", "")).lower() in ("1", "on", "true", "yes")
    errors = {}
    if len(data["holder_name"]) < 2:
        errors["holder_name"] = "Your name is required."
    if not _EMAIL.match(data["email"]):
        errors["email"] = "A valid email address is required; the copy of this notice goes there."
    if data["rights_type"] not in dict(RIGHTS_TYPES):
        errors["rights_type"] = "Say whether this is a trademark, a copyright, or both."
    if len(data["mark_or_work"]) < 2:
        errors["mark_or_work"] = "Name the registered trademark or the copyrighted work."
    if not (_URL.search(data["listing_urls"]) or _CODE.search(data["listing_urls"])):
        errors["listing_urls"] = "Give at least one listing URL on the Website (https://...) or a product code."
    if len(data["reason"]) < 20:
        errors["reason"] = "Explain, in a few sentences, why you believe the listing infringes your rights."
    if data["action_requested"] not in dict(ACTIONS):
        errors["action_requested"] = "Choose the action you are requesting."
    if data["action_requested"] == "other" and len(data["action_detail"]) < 5:
        errors["action_detail"] = "Say what action you are requesting."
    if not data["declaration"]:
        errors["declaration"] = "The good-faith statement is required."
    if not data["hold_agreed"]:
        errors["hold_agreed"] = "The notice procedure and the hold period are part of the Terms; please confirm."
    return data, errors


def working_days_after(start, days):
    """``days`` working days after ``start`` (Sundays skipped), as a date."""
    day = start.date() if isinstance(start, datetime.datetime) else start
    left = days
    while left > 0:
        day += datetime.timedelta(days=1)
        if day.weekday() != 6:
            left -= 1
    return day


def dates(now=None):
    """``(ack_due, hold_until)`` for a notice received now."""
    now = now or datetime.datetime.now()
    ack_due = working_days_after(now, ACK_WORKING_DAYS)
    return ack_due, ack_due + datetime.timedelta(days=HOLD_DAYS)


def reference(now, email):
    """``IPN-YYYYMMDD-XXXX``: the day plus four hex characters from the time and mailbox."""
    now = now or datetime.datetime.now()
    digest = hashlib.sha256(("%s|%s" % (now.isoformat(timespec="microseconds"), email)).encode("utf-8")).hexdigest()
    return "IPN-%s-%s" % (now.strftime("%Y%m%d"), digest[:4].upper())


def notice_text(data, ref, ack_due, hold_until, received=None, site="optiwar.in"):
    """The notice in full, as plain text: the same copy for Optiwar and the Rights Holder."""
    received = received or datetime.datetime.now()
    lines = [
        "TRADEMARK / COPYRIGHT HOLDER NOTICE TO OPTIWAR",
        "Reference: %s" % ref,
        "Received: %s" % received.strftime("%A, %d %B %Y %H:%M"),
        "Website: %s" % site,
        "",
        "RIGHTS HOLDER",
        "Name: %s" % data.get("holder_name", ""),
        "Company: %s" % (data.get("company") or "-"),
        "Designation: %s" % (data.get("designation") or "-"),
        "Email: %s" % data.get("email", ""),
        "Phone: %s" % (data.get("phone") or "-"),
        "Address: %s" % (data.get("address") or "-"),
        "",
        "THE RIGHT",
        "Type: %s" % dict(RIGHTS_TYPES).get(data.get("rights_type"), data.get("rights_type", "")),
        "Trademark / work: %s" % data.get("mark_or_work", ""),
        "Registration / proof of ownership: %s" % (data.get("registration") or "-"),
        "",
        "THE LISTING(S)",
        data.get("listing_urls", ""),
        "",
        "WHY IT INFRINGES (as stated by the Rights Holder)",
        data.get("reason", ""),
        "",
        "ACTION REQUESTED",
        dict(ACTIONS).get(data.get("action_requested"), data.get("action_requested", "")),
    ]
    if data.get("action_detail"):
        lines.append(data["action_detail"])
    lines += [
        "",
        "DECLARATION",
        "The Rights Holder states, in good faith, that the information above is accurate and that they are the "
        "owner of the right concerned or authorised to act for the owner.",
        "The Rights Holder confirms the notice procedure and the hold period in Optiwar's Terms and Conditions "
        "(Trademark and Copyright Holder Notices; Governing Law and Jurisdiction: New Delhi and Gurugram courts).",
        "",
        "WHAT HAPPENS NEXT",
        "Optiwar acknowledges a complete notice within %d working days (by %s)." % (ACK_WORKING_DAYS, ack_due.strftime("%A, %d %B %Y")),
        "The %d-day hold period runs from the acknowledgement; the earliest day it can end is %s." % (HOLD_DAYS, hold_until.strftime("%A, %d %B %Y")),
        "During the hold period Optiwar verifies the listing and the sourcing of the goods it holds in inventory "
        "and may correct, suspend or remove it, or explain in writing why it considers the listing lawful.",
        "",
        "Krishna Ecommerce Technologies Private Limited, Godown Village Fazilpur, Badshahpur Tethar(222), "
        "Badshahpur, Gurgaon-122101, India.",
    ]
    return "\n".join(lines)


def holder_mail_text(data, ref, ack_due, hold_until, text):
    """The courteous acknowledgement the Rights Holder receives, with the notice copy below it."""
    name = (data.get("holder_name") or "").strip() or "Sir/Madam"
    return (
        "Dear %s,\n\n"
        "Thank you for writing to Optiwar. We have received your trademark / copyright notice and recorded it "
        "under reference %s. Please quote this reference in any further correspondence.\n\n"
        "What happens next:\n"
        "- We will acknowledge and begin our review within %d working days (by %s).\n"
        "- We then verify the listing and the sourcing of the goods we hold in inventory, and reply to you in "
        "writing. The hold period in our Terms runs for up to %d days from the acknowledgement (to %s at the "
        "latest); in practice we aim to reply well before that.\n"
        "- We kindly ask you to hold any further action until you hear from us, as our Terms provide. If anything "
        "in this notice is urgent or changes, simply reply to this email.\n\n"
        "We take brand and copyright concerns seriously and appreciate the courtesy of this notice.\n\n"
        "Warm regards,\n"
        "Optiwar Legal and Assurance Team\n"
        "Krishna Ecommerce Technologies Private Limited\n"
        "Godown Village Fazilpur, Badshahpur Tethar(222), Badshahpur, Gurgaon-122101, India\n\n"
        "----- Copy of your notice -----\n%s\n"
    ) % (name, ref, ACK_WORKING_DAYS, ack_due.strftime("%A, %d %B %Y"), HOLD_DAYS,
         hold_until.strftime("%A, %d %B %Y"), text)


def prompt_section():
    """How the Optiwar assistant handles a brand / copyright complaint in chat (chat_gateway)."""
    return (
        "\n\nTRADEMARK OR COPYRIGHT COMPLAINTS: if the visitor says they own a brand, trademark or copyright and "
        "object to a product, image or text on Optiwar, be courteous and thank them; never argue about the "
        "merits or promise an outcome. Optiwar is an inventory-led, direct-to-consumer factory outlet (not a "
        "marketplace), and our Terms ask rights holders to send a written notice first. Point them to the "
        "notice form at %s (it emails the notice to our team and a copy to them, and gives a reference), or "
        "the postal address in the Terms. Explain that we acknowledge within %d working days, then review the "
        "listing and the sourcing of the goods we hold and reply in writing; the hold period is up to %d days "
        "from the acknowledgement and we usually reply much sooner. Kindly ask them to hold any further action "
        "until they hear from us. Do not collect the complaint in the chat yourself and do not quote any email "
        "address other than admin@optiwar.com."
    ) % (PATH, ACK_WORKING_DAYS, HOLD_DAYS)


def store(cursor, ref, data, ack_due, hold_until, now=None, ip=None, site=None, mail_sent=False):
    """Insert the notice; False (never raises) when the table is missing."""
    try:
        cursor.execute(
            "INSERT INTO ip_notices (reference, created_at, holder_name, company, designation, email, phone, "
            "address, rights_type, mark_or_work, registration, listing_urls, reason, action_requested, "
            "action_detail, ack_due, hold_until, mail_sent, ip, site) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ref, now or datetime.datetime.now(), data["holder_name"], data.get("company") or None,
             data.get("designation") or None, data["email"], data.get("phone") or None,
             data.get("address") or None, data["rights_type"], data["mark_or_work"],
             data.get("registration") or None, data["listing_urls"], data["reason"],
             data["action_requested"], data.get("action_detail") or None, ack_due, hold_until,
             1 if mail_sent else 0, ip, site))
        return True
    except Exception:  # noqa: BLE001 - the acknowledgement still shows the reference
        return False


def mark_mailed(cursor, ref):
    try:
        cursor.execute("UPDATE ip_notices SET mail_sent = 1 WHERE reference = %s", (ref,))
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_schema(db):
    try:
        cur = db.cursor()
        for _name, ddl in TABLES:
            cur.execute(ddl)
        db.commit()
    except Exception:  # noqa: BLE001
        pass


def register(bp):
    """GET shows the form; POST validates, stores, mails both parties, acknowledges."""
    from flask import current_app, render_template, request, session  # noqa: PLC0415

    def _ctx(data=None, errors=None, **extra):
        ack_due, hold_until = dates()
        ctx = {"data": data or {f: "" for f in FIELDS}, "errors": errors or {},
               "rights_types": RIGHTS_TYPES, "actions": ACTIONS,
               "ack_days": ACK_WORKING_DAYS, "hold_days": HOLD_DAYS,
               "ack_due": ack_due, "hold_until": hold_until}
        ctx.update(extra)
        return ctx

    @bp.route(PATH, methods=["GET"])
    def ip_notice():
        return render_template("ip_notice.html", **_ctx())

    @bp.route(PATH, methods=["POST"])
    def ip_notice_submit():
        if request.form.get("website_url"):           # honeypot: bots fill every field
            return render_template("ip_notice.html", **_ctx()), 400
        data, errors = parse(request.form)
        if errors:
            return render_template("ip_notice.html", **_ctx(data, errors)), 400
        used = int(session.get(SESSION_COUNT_KEY) or 0)
        if used >= MAX_PER_SESSION:
            errors = {"form": "You have sent a few notices already. Please wait for our acknowledgement, "
                              "or write to us at the address in the Terms."}
            return render_template("ip_notice.html", **_ctx(data, errors)), 429
        now = datetime.datetime.now()
        ref = reference(now, data["email"])
        ack_due, hold_until = dates(now)
        site = request.host or ""
        text = notice_text(data, ref, ack_due, hold_until, received=now, site=site)
        stored = False
        try:
            from .db import get_db  # noqa: PLC0415
            db = get_db()
            ensure_schema(db)
            cursor = db.cursor()
            stored = store(cursor, ref, data, ack_due, hold_until, now=now,
                           ip=request.headers.get("X-Forwarded-For", request.remote_addr), site=site)
            db.commit()
        except Exception:  # noqa: BLE001
            current_app.logger.exception("ip_notice: store failed ref=%s", ref)
        sent = False
        try:
            from .notice_mail import send_ip_notice  # noqa: PLC0415
            recipients = tuple(current_app.config.get("IP_NOTICE_RECIPIENTS") or RECIPIENTS)
            sent = send_ip_notice(ref, data["email"], data["holder_name"], text,
                                  holder_mail_text(data, ref, ack_due, hold_until, text), recipients)
            if sent and stored:
                try:
                    from .db import get_db  # noqa: PLC0415
                    db = get_db()
                    mark_mailed(db.cursor(), ref)
                    db.commit()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            current_app.logger.exception("ip_notice: mail failed ref=%s", ref)
        if not stored and not sent:
            # Neither recorded nor mailed: saying "received" would lose the notice.
            current_app.logger.error("ip_notice: LOST ref=%s (not stored, not mailed)", ref)
            return render_template("ip_notice.html", **_ctx(data, {
                "form": "We could not record your notice just now, so nothing has been sent. Please try again in "
                        "a few minutes, or write to admin@optiwar.com or the postal address in the Terms."})), 503
        session[SESSION_COUNT_KEY] = used + 1
        session.modified = True
        current_app.logger.info("ACTIVITY:IP_NOTICE ref=%s stored=%s mailed=%s site=%s", ref, stored, sent, site)
        return render_template("ip_notice.html", **_ctx(data, {}, sent=True, ref=ref, mailed=sent,
                                                          stored=stored, notice_text=text,
                                                          ack_due=ack_due, hold_until=hold_until))
