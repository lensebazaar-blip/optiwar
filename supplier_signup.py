"""Become a supplier: a manufacturer, wholesaler, distributor or importer offers
to supply Optiwar's inventory (Optiwar buys and holds stock; it is not a
marketplace). The form takes the business, the GST registration and where it
is, the factory or warehouse, what they make or carry, and how to reach them.

* **GSTIN is checked, not just typed**: 15 characters, state code, PAN
  shape, entity code, 'Z', and the checksum character; the GST state is read
  from the number and must match the state the supplier selected.
* **Stored first** (``supplier_applications``, additive), **then mailed**:
  the application to Optiwar's admin mailboxes and a signup confirmation
  letter to the supplier with a reference and what happens next.

Stdlib at import; Flask, the DB and the mailer are imported inside the
functions that need them, so the deploy tool can read ``TABLES`` plainly.
"""
import datetime
import hashlib
import re

PATH = "/become-a-supplier"
RECIPIENTS = ("admin@optiwar.com", "admin@lensbazaar.com")
REVIEW_WORKING_DAYS = 7
MAX_PER_SESSION = 3
SESSION_COUNT_KEY = "supplier_signup_count"
MAX_TEXT = 3000

BUSINESS_TYPES = (("manufacturer", "Manufacturer / factory"), ("wholesaler", "Wholesaler"),
                  ("distributor", "Authorised distributor"), ("importer", "Importer"),
                  ("brand", "Brand owner"))
CATEGORIES = (("frames", "Spectacle frames"), ("sunglasses", "Sunglasses"), ("lenses", "Ophthalmic lenses"),
              ("contact_lenses", "Contact lenses"), ("smart", "Smart / AI eyewear"),
              ("accessories", "Cases, cloths, accessories"), ("other", "Other"))

# GST state codes (as on the first two characters of a GSTIN).
GST_STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh",
    "05": "Uttarakhand", "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh",
    "10": "Bihar", "11": "Sikkim", "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam", "19": "West Bengal",
    "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka", "30": "Goa",
    "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory", "99": "Centre Jurisdiction",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS supplier_applications (
    application_id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    reference VARCHAR(24) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL,
    business_name VARCHAR(160) NOT NULL,
    business_type VARCHAR(24) NOT NULL,
    contact_name VARCHAR(120) NOT NULL,
    designation VARCHAR(120) NULL,
    email VARCHAR(190) NOT NULL,
    phone VARCHAR(40) NOT NULL,
    website VARCHAR(200) NULL,
    gstin CHAR(15) NOT NULL,
    gst_state VARCHAR(80) NOT NULL,
    pan CHAR(10) NOT NULL,
    address VARCHAR(400) NOT NULL,
    city VARCHAR(120) NOT NULL,
    state VARCHAR(80) NOT NULL,
    pincode VARCHAR(10) NULL,
    factory_address VARCHAR(400) NULL,
    categories VARCHAR(200) NOT NULL,
    brands VARCHAR(400) NULL,
    capacity VARCHAR(200) NULL,
    years_in_business VARCHAR(10) NULL,
    message TEXT NULL,
    mail_sent TINYINT(1) NOT NULL DEFAULT 0,
    status VARCHAR(16) NOT NULL DEFAULT 'NEW',
    ip VARCHAR(64) NULL,
    site VARCHAR(32) NULL,
    KEY ix_sup_created (created_at),
    KEY ix_sup_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
TABLES = (("supplier_applications", SCHEMA),)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GSTIN = re.compile(r"^(\d{2})([A-Z]{5}\d{4}[A-Z])([0-9A-Z])Z([0-9A-Z])$")
_PHONE = re.compile(r"^\+?[\d\s\-()]{8,20}$")
_ALNUM = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

TEXT_FIELDS = ("business_name", "business_type", "contact_name", "designation", "email", "phone", "website",
               "gstin", "address", "city", "state", "pincode", "factory_address", "brands", "capacity",
               "years_in_business", "message")


def gstin_checksum(first14):
    """The 15th character the GST network expects for the first fourteen."""
    total = 0
    for i, ch in enumerate(first14):
        value = _ALNUM.index(ch)
        product = value * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _ALNUM[(36 - total % 36) % 36]


def parse_gstin(raw):
    """``(gstin, state, pan, error)``: the number cleaned and checked."""
    g = re.sub(r"\s+", "", (raw or "")).upper()
    if not g:
        return "", "", "", "A GSTIN is required."
    m = _GSTIN.match(g)
    if not m:
        return g, "", "", "That does not look like a GSTIN (15 characters, e.g. 06AAHCK4567A1ZP)."
    state = GST_STATES.get(m.group(1))
    if not state:
        return g, "", "", "The first two digits of the GSTIN are not a GST state code."
    if gstin_checksum(g[:14]) != g[14]:
        return g, state, m.group(2), "The GSTIN's last character does not match its checksum; please re-check it."
    return g, state, m.group(2), ""


def _clean(value, limit):
    return re.sub(r"[ \t]+", " ", (value or "").strip())[:limit]


def parse(form):
    """``(data, errors)``: the application as typed, cleaned and checked."""
    data = {f: _clean(form.get(f, ""), MAX_TEXT if f in ("address", "factory_address", "brands", "message") else 200)
            for f in TEXT_FIELDS}
    getlist = getattr(form, "getlist", None)
    cats = getlist("categories") if getlist else form.get("categories", [])
    if isinstance(cats, str):
        cats = [cats]
    data["categories"] = [c for c in cats if c in dict(CATEGORIES)]
    data["accept"] = str(form.get("accept", "")).lower() in ("1", "on", "true", "yes")
    errors = {}
    if len(data["business_name"]) < 2:
        errors["business_name"] = "The business name is required."
    if data["business_type"] not in dict(BUSINESS_TYPES):
        errors["business_type"] = "Choose what kind of business you are."
    if len(data["contact_name"]) < 2:
        errors["contact_name"] = "A contact person is required."
    if not _EMAIL.match(data["email"]):
        errors["email"] = "A valid email address is required; the confirmation letter goes there."
    if not _PHONE.match(data["phone"]):
        errors["phone"] = "A phone number is required."
    gstin, gst_state, pan, g_err = parse_gstin(data["gstin"])
    data["gstin"], data["gst_state"], data["pan"] = gstin, gst_state, pan
    if g_err:
        errors["gstin"] = g_err
    if len(data["address"]) < 8:
        errors["address"] = "The registered business address is required."
    if len(data["city"]) < 2:
        errors["city"] = "City is required."
    if len(data["state"]) < 2:
        errors["state"] = "State is required."
    elif gst_state and data["state"].strip().lower() != gst_state.lower():
        errors["state"] = "The GSTIN is registered in %s; select that state, or check the GSTIN." % gst_state
    if not data["categories"]:
        errors["categories"] = "Tick at least one product category."
    if not data["accept"]:
        errors["accept"] = "Please confirm the details are true and that we may contact you."
    return data, errors


def reference(now, email):
    now = now or datetime.datetime.now()
    digest = hashlib.sha256(("%s|%s" % (now.isoformat(timespec="microseconds"), email)).encode("utf-8")).hexdigest()
    return "SUP-%s-%s" % (now.strftime("%Y%m%d"), digest[:4].upper())


def working_days_after(start, days):
    day = start.date() if isinstance(start, datetime.datetime) else start
    left = days
    while left > 0:
        day += datetime.timedelta(days=1)
        if day.weekday() != 6:
            left -= 1
    return day


def application_text(data, ref, received=None, site="optiwar.in"):
    """The application in full, as plain text, for the admin mail and the letter."""
    received = received or datetime.datetime.now()
    cats = ", ".join(dict(CATEGORIES).get(c, c) for c in data.get("categories", [])) or "-"
    lines = [
        "SUPPLIER APPLICATION TO OPTIWAR",
        "Reference: %s" % ref,
        "Received: %s" % received.strftime("%A, %d %B %Y %H:%M"),
        "Website: %s" % site,
        "",
        "BUSINESS",
        "Name: %s" % data.get("business_name", ""),
        "Type: %s" % dict(BUSINESS_TYPES).get(data.get("business_type"), data.get("business_type", "")),
        "Website: %s" % (data.get("website") or "-"),
        "Years in business: %s" % (data.get("years_in_business") or "-"),
        "",
        "GST",
        "GSTIN: %s" % data.get("gstin", ""),
        "GST state (from the GSTIN): %s" % data.get("gst_state", ""),
        "PAN (from the GSTIN): %s" % data.get("pan", ""),
        "",
        "ADDRESSES",
        "Registered address: %s, %s, %s %s" % (data.get("address", ""), data.get("city", ""), data.get("state", ""), data.get("pincode") or ""),
        "Factory / warehouse: %s" % (data.get("factory_address") or "same as registered"),
        "",
        "WHAT THEY SUPPLY",
        "Categories: %s" % cats,
        "Brands / lines: %s" % (data.get("brands") or "-"),
        "Monthly capacity / MOQ: %s" % (data.get("capacity") or "-"),
        "",
        "CONTACT",
        "Name: %s" % data.get("contact_name", ""),
        "Designation: %s" % (data.get("designation") or "-"),
        "Email: %s" % data.get("email", ""),
        "Phone: %s" % data.get("phone", ""),
        "",
        "MESSAGE",
        data.get("message") or "-",
    ]
    return "\n".join(lines)


def letter_text(data, ref, review_by, text):
    """The signup confirmation letter the supplier receives."""
    name = (data.get("contact_name") or "").strip() or "Sir/Madam"
    return (
        "Dear %s,\n\n"
        "Thank you for offering to supply Optiwar. We have received the application for %s and recorded it "
        "under reference %s. Please quote this reference in any correspondence.\n\n"
        "What happens next:\n"
        "- Our sourcing team reviews every application and will write to you within %d working days (by %s).\n"
        "- Please keep ready: your GST registration certificate, PAN, a cancelled cheque or bank letter, your "
        "product catalogue with wholesale price list, and samples of the lines you propose.\n"
        "- Optiwar buys and holds its own inventory and sells directly to consumers; we are not a marketplace. "
        "Terms of purchase, payment cycles and quality checks are agreed in writing before the first order.\n\n"
        "If anything in the application changes, simply reply to this email.\n\n"
        "Warm regards,\n"
        "Optiwar Sourcing Team\n"
        "Krishna Ecommerce Technologies Private Limited\n"
        "Godown Village Fazilpur, Badshahpur Tethar(222), Badshahpur, Gurgaon-122101, India\n\n"
        "----- Copy of your application -----\n%s\n"
    ) % (name, data.get("business_name", ""), ref, REVIEW_WORKING_DAYS, review_by.strftime("%A, %d %B %Y"), text)


def store(cursor, ref, data, now=None, ip=None, site=None):
    try:
        cursor.execute(
            "INSERT INTO supplier_applications (reference, created_at, business_name, business_type, contact_name, "
            "designation, email, phone, website, gstin, gst_state, pan, address, city, state, pincode, "
            "factory_address, categories, brands, capacity, years_in_business, message, ip, site) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ref, now or datetime.datetime.now(), data["business_name"], data["business_type"], data["contact_name"],
             data.get("designation") or None, data["email"], data["phone"], data.get("website") or None,
             data["gstin"], data["gst_state"], data["pan"], data["address"], data["city"], data["state"],
             data.get("pincode") or None, data.get("factory_address") or None, ",".join(data["categories"]),
             data.get("brands") or None, data.get("capacity") or None, data.get("years_in_business") or None,
             data.get("message") or None, ip, site))
        return True
    except Exception:  # noqa: BLE001
        return False


def mark_mailed(cursor, ref):
    try:
        cursor.execute("UPDATE supplier_applications SET mail_sent = 1 WHERE reference = %s", (ref,))
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
    from flask import current_app, render_template, request, session  # noqa: PLC0415

    def _ctx(data=None, errors=None, **extra):
        ctx = {"data": data or dict({f: "" for f in TEXT_FIELDS}, categories=[]), "errors": errors or {},
               "business_types": BUSINESS_TYPES, "categories": CATEGORIES,
               "states": sorted(set(GST_STATES.values())), "review_days": REVIEW_WORKING_DAYS}
        ctx.update(extra)
        return ctx

    @bp.route(PATH, methods=["GET"])
    def supplier_signup():
        return render_template("supplier_signup.html", **_ctx())

    @bp.route(PATH, methods=["POST"])
    def supplier_signup_submit():
        if request.form.get("website_url"):           # honeypot
            return render_template("supplier_signup.html", **_ctx()), 400
        data, errors = parse(request.form)
        if errors:
            return render_template("supplier_signup.html", **_ctx(data, errors)), 400
        used = int(session.get(SESSION_COUNT_KEY) or 0)
        if used >= MAX_PER_SESSION:
            return render_template("supplier_signup.html", **_ctx(data, {"form": "You have sent a few applications "
                                   "already. Please wait for our reply, or write to admin@optiwar.com."})), 429
        now = datetime.datetime.now()
        ref = reference(now, data["email"])
        review_by = working_days_after(now, REVIEW_WORKING_DAYS)
        site = request.host or ""
        text = application_text(data, ref, received=now, site=site)
        letter = letter_text(data, ref, review_by, text)
        stored = False
        try:
            from .db import get_db  # noqa: PLC0415
            db = get_db()
            ensure_schema(db)
            stored = store(db.cursor(), ref, data, now=now,
                           ip=request.headers.get("X-Forwarded-For", request.remote_addr), site=site)
            db.commit()
        except Exception:  # noqa: BLE001
            current_app.logger.exception("supplier_signup: store failed ref=%s", ref)
        sent = False
        try:
            from .mail import send_supplier_signup  # noqa: PLC0415
            recipients = tuple(current_app.config.get("SUPPLIER_SIGNUP_RECIPIENTS") or RECIPIENTS)
            sent = send_supplier_signup(ref, data["email"], data["business_name"], text, letter, recipients)
            if sent and stored:
                try:
                    from .db import get_db  # noqa: PLC0415
                    db = get_db()
                    mark_mailed(db.cursor(), ref)
                    db.commit()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            current_app.logger.exception("supplier_signup: mail failed ref=%s", ref)
        session[SESSION_COUNT_KEY] = used + 1
        session.modified = True
        current_app.logger.info("ACTIVITY:SUPPLIER_SIGNUP ref=%s gst_state=%s stored=%s mailed=%s site=%s",
                                ref, data["gst_state"], stored, sent, site)
        return render_template("supplier_signup.html", **_ctx(data, {}, sent=True, ref=ref, mailed=sent,
                                                              stored=stored, letter=letter, review_by=review_by))
