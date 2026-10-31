"""AI Glasses (Ray-Ban Meta) on optiwar.in: a separate product line with its own lens set.

What makes this vertical different from Optiwar's frames, and why it is one module:

* It is sold on optiwar.in only (``sell_on_in = 1``, ``sell_on_com = 0``) and only
  once ``AI_GLASSES_IN_ENABLED=1`` (systemd drop-in, default off). Until then the
  owner sees it through a signed, expiring, noindex preview link.
* It never uses Optiwar's free-lens scheme or ``config/lens_pricing.json``. A pair is
  sold as it comes (non-prescription) or with one of its own lens options below.
* Prescription limits follow the manufacturer's matrix per model (``rx_allowed`` and
  the total-power range in ``ai_glasses_products``), not Optiwar's frame dropdowns.
* Stock is fixed: quantity is never reduced (paid_orders skips the vertical). Staff
  set a sales status per product instead: PREORDER (default), SELLING_FAST, SOLD_OUT.

Model data lives in ``ai_glasses_products`` (one row per sellable product, keyed by
``products.product_id``); price, name, images and HSN stay on ``products``.
"""
import datetime
import os

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)

bp = Blueprint('ai_glasses', __name__)

VERTICAL = "SMART_EYEWEAR"
PAGE_PATH = "/ai-glasses"
FLAG = "AI_GLASSES_IN_ENABLED"

HSN_GLASSES = "85176290"   # 18%
HSN_LENSES = "90015000"    # 5%, the lens line on the Ops invoice

PREORDER, SELLING_FAST, SOLD_OUT = "PREORDER", "SELLING_FAST", "SOLD_OUT"
SALES_STATUSES = (PREORDER, SELLING_FAST, SOLD_OUT)
PREORDER_DAYS = 30
SHIP_DAYS = (3, 7)          # dispatch window shown for SELLING_FAST

# The AI Glasses lens set (INR, GST inclusive). Prescription prices are the
# owner's placeholders until confirmed.
LENSES = (
    {"key": "none", "label": "Non-prescription", "help": "Lenses as supplied by Ray-Ban Meta.", "price": 0, "rx": False},
    {"key": "single", "label": "Single vision", "help": "One power for distance or reading. Fitted by our opticians.", "price": 4900, "rx": True},
    {"key": "progressive", "label": "Progressive", "help": "Distance, intermediate and near in one lens. Fitted by our opticians.", "price": 9900, "rx": True},
)
LENS_BY_KEY = {lens["key"]: lens for lens in LENSES}

# Prescription entry limits that are not part of the manufacturer matrix
# (per-model total power is). Opticians confirm these before launch.
RX_SPH = (-12.0, 6.0)
RX_CYL = (-6.0, 0.0)
RX_ADD = (0.75, 3.5)
RX_PD = (50, 80)
RX_STEP = 0.25
RX_AXIS_STEP = 5   # AXIS 5-180 in steps of 5, as on Optiwar frames

PREVIEW_DAYS = 7
PREVIEW_SALT = "ai-glasses-preview"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_glasses_products (
    product_id INT NOT NULL PRIMARY KEY,
    family VARCHAR(40) NOT NULL,
    family_title VARCHAR(60) NOT NULL,
    style_code VARCHAR(10) NOT NULL,
    frame_colour VARCHAR(40) NOT NULL,
    lens_colour VARCHAR(60) NOT NULL,
    size_label VARCHAR(12) NOT NULL,
    size_spec VARCHAR(16) NOT NULL,
    brand VARCHAR(60) NOT NULL,
    mpn VARCHAR(40) NOT NULL,
    gtin VARCHAR(14) NULL,
    rx_allowed TINYINT(1) NOT NULL DEFAULT 0,
    rx_total_min DECIMAL(5,2) NULL,
    rx_total_max DECIMAL(5,2) NULL,
    sales_status VARCHAR(16) NOT NULL DEFAULT 'PREORDER',
    sort_order INT NOT NULL DEFAULT 0,
    updated_at DATETIME NULL,
    updated_by VARCHAR(100) NULL,
    KEY ix_aig_family (family, sort_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

STATUS_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_glasses_status_log (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    product_id INT NOT NULL,
    old_status VARCHAR(16) NULL,
    new_status VARCHAR(16) NOT NULL,
    changed_by VARCHAR(100) NOT NULL,
    changed_at DATETIME NOT NULL,
    KEY ix_aig_log_product (product_id, changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("ai_glasses_products", SCHEMA), ("ai_glasses_status_log", STATUS_LOG_SCHEMA))

_SCHEMA_READY = False


def ensure_schema(db):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    cur = db.cursor()
    for _name, ddl in TABLES:
        cur.execute(ddl)
    db.commit()
    _SCHEMA_READY = True


def _db():
    from .db import get_db
    db = get_db()
    ensure_schema(db)
    return db


# ---- visibility -----------------------------------------------------------------

def _on_india_site():
    """optiwar.in (or legacy in.optiwar.com): the same host test as catalogue.site_from_host,
    kept local so this module has no import cycle with catalogue.py."""
    try:
        host = (request.host or "").lower()
    except RuntimeError:
        return False
    return "in.optiwar.com" in host or "optiwar.in" in host


def is_live():
    """The release flag: AI Glasses are public on optiwar.in."""
    return str(os.environ.get(FLAG, current_app.config.get(FLAG, "")) or "") == "1"


def _serializer():
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=PREVIEW_SALT)


def preview_active():
    until = session.get("aig_preview_until")
    try:
        return bool(until) and datetime.datetime.utcnow().timestamp() < float(until)
    except (TypeError, ValueError):
        return False


def is_visible():
    """Whether this request may see AI Glasses: optiwar.in, and live or previewing."""
    try:
        return _on_india_site() and (is_live() or preview_active())
    except RuntimeError:
        return False


# ---- data -----------------------------------------------------------------------

_SELECT = """
SELECT p.product_id, p.product_code, p.product_name, p.product_details, p.product_price,
       p.product_special_price, p.product_image, p.product_slug, p.hsn, p.product_status,
       a.family, a.family_title, a.style_code, a.frame_colour, a.lens_colour, a.size_label,
       a.size_spec, a.brand, a.mpn, a.gtin, a.rx_allowed, a.rx_total_min, a.rx_total_max,
       a.sales_status, a.sort_order
FROM products p JOIN ai_glasses_products a ON a.product_id = p.product_id
WHERE p.product_vertical = %s AND p.sell_on_in = 1 AND p.product_status = 'ACTIVE'
"""


def _images(row):
    out = []
    for part in (row.get("product_image") or "").split(","):
        part = part.strip()
        if part:
            out.append(url_for("static", filename=part.lstrip("./")))
    return out


def _dispatch(row, today=None):
    today = today or datetime.date.today()
    status = row.get("sales_status") or PREORDER
    if status == SOLD_OUT:
        return {"status": status, "badge": "Sold out", "text": "Sold out", "date": None, "can_buy": False}
    if status == SELLING_FAST:
        return {"status": status, "badge": "Selling fast",
                "text": "Selling fast: dispatched in %s-%s days" % SHIP_DAYS, "date": None, "can_buy": True}
    date = today + datetime.timedelta(days=PREORDER_DAYS)
    return {"status": PREORDER, "badge": "Pre-order",
            "text": "Pre-order: dispatch by %s" % date.strftime("%a, %d %b"),
            "date": date.isoformat(), "can_buy": True}


def _price(row):
    mrp = int(row.get("product_price") or 0)
    sell = int(row.get("product_special_price") or 0) or mrp
    sell = min(sell, mrp) if mrp else sell
    off = int(round((mrp - sell) * 100.0 / mrp)) if mrp and sell < mrp else 0
    return {"mrp": mrp, "price": sell, "off": off}


def _decorate(row):
    row = dict(row)
    row.update(_price(row))
    row["dispatch"] = _dispatch(row)
    row["images"] = _images(row)
    row["url"] = url_for("ai_glasses.product", slug=row["product_slug"])
    return row


def load_products(cursor):
    cursor.execute(_SELECT + " ORDER BY a.family, a.sort_order, p.product_id", (VERTICAL,))
    return [_decorate(r) for r in cursor.fetchall()]


def load_product(cursor, slug):
    cursor.execute(_SELECT + " AND p.product_slug = %s", (VERTICAL, slug))
    row = cursor.fetchone()
    return _decorate(row) if row else None


def listing(products):
    """Families -> styles (one card each) -> sizes (one product each)."""
    families = {}
    for p in products:
        fam = families.setdefault(p["family"], {"code": p["family"], "title": p["family_title"],
                                               "brand": p["brand"], "styles": {}, "rx": False,
                                               "price_from": p["price"]})
        fam["price_from"] = min(fam["price_from"], p["price"])
        fam["rx"] = fam["rx"] or bool(p["rx_allowed"])
        style = fam["styles"].setdefault(p["style_code"], {"code": p["style_code"],
                                                          "title": "%s · %s" % (p["frame_colour"], p["lens_colour"]),
                                                          "sizes": []})
        style["sizes"].append(p)
    out = []
    for fam in families.values():
        fam["styles"] = list(fam["styles"].values())
        out.append(fam)
    return out


def money(value):
    value = int(round(float(value or 0)))
    s = str(abs(value))
    if len(s) > 3:   # Indian grouping: 1,23,456
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if value < 0 else "") + "₹" + s


@bp.app_template_filter("aig_money")
def _aig_money(value):
    return money(value)


@bp.app_context_processor
def _inject():
    try:
        return {"ai_glasses_nav": is_visible()}
    except Exception:
        return {"ai_glasses_nav": False}


# ---- prescription ----------------------------------------------------------------

def _num(form, name):
    raw = (form.get(name) or "").strip().replace("+", "")
    if raw == "":
        return 0.0
    return float(raw)


def _on_step(value):
    return abs(value / RX_STEP - round(value / RX_STEP)) < 1e-6


def validate_rx(form, product, lens):
    """Server-side check of the prescription for this model and lens.

    Returns (rx, errors). rx holds normalised values per eye plus PD; errors are
    customer-facing sentences. The total power (SPH + CYL) range is the
    manufacturer's, stored per model.
    """
    errors = []
    rx = {}
    tmin = float(product.get("rx_total_min") if product.get("rx_total_min") is not None else RX_SPH[0])
    tmax = float(product.get("rx_total_max") if product.get("rx_total_max") is not None else RX_SPH[1])
    for eye, name in (("right", "Right eye (OD)"), ("left", "Left eye (OS)")):
        try:
            sph = _num(form, eye + "_sph")
            cyl = _num(form, eye + "_cyl")
            axis = int(_num(form, eye + "_axis"))
            add = _num(form, eye + "_add")
        except ValueError:
            errors.append("%s: please enter numbers only." % name)
            continue
        if not (RX_SPH[0] <= sph <= RX_SPH[1]) or not _on_step(sph):
            errors.append("%s: SPH must be between %+.2f and %+.2f in 0.25 steps." % (name, RX_SPH[0], RX_SPH[1]))
        if not (RX_CYL[0] <= cyl <= RX_CYL[1]) or not _on_step(cyl):
            errors.append("%s: CYL must be between %+.2f and %.2f in 0.25 steps." % (name, RX_CYL[0], RX_CYL[1]))
        if cyl != 0 and not (RX_AXIS_STEP <= axis <= 180 and axis % RX_AXIS_STEP == 0):
            errors.append("%s: AXIS (5-180, in steps of 5) is required when CYL is not 0." % name)
        if not (tmin <= sph + cyl <= tmax):
            errors.append("%s: total power (SPH + CYL) %+.2f is outside %+.2f to %+.2f for this model. "
                          "Please contact us before ordering." % (name, sph + cyl, tmin, tmax))
        if lens["key"] == "progressive":
            if not (RX_ADD[0] <= add <= RX_ADD[1]) or not _on_step(add):
                errors.append("%s: ADD must be between +%.2f and +%.2f for progressive lenses." % (name, RX_ADD[0], RX_ADD[1]))
        else:
            add = 0.0
        rx[eye] = {"sph": sph, "cyl": cyl, "axis": axis if cyl else 0, "add": add}
    try:
        pd = float((form.get("pd") or "0").strip())
    except ValueError:
        pd = 0
    if not (RX_PD[0] <= pd <= RX_PD[1]):
        errors.append("Pupillary distance (PD) must be between %s and %s mm." % RX_PD)
    rx["pd"] = pd
    return rx, errors


def _eye(r):
    return "%s/%s/%s/%s" % (r["sph"], r["cyl"], r["axis"], r["add"])


# ---- pages ------------------------------------------------------------------------

def _guard():
    if not is_visible():
        abort(404)


def _page_robots():
    return "index, follow" if is_live() else "noindex, nofollow"


@bp.route(PAGE_PATH)
def landing():
    _guard()
    cur = _db().cursor()
    try:
        products = load_products(cur)
    finally:
        cur.close()
    return render_template("ai_glasses/landing.html", families=listing(products),
                           products=products, robots=_page_robots(),
                           offer_off=max([p["off"] for p in products] or [0]),
                           preview=not is_live())


@bp.route(PAGE_PATH + "/<slug>")
def product(slug):
    _guard()
    cur = _db().cursor()
    try:
        item = load_product(cur, slug)
        if not item:
            abort(404)
        family = [p for p in load_products(cur) if p["family"] == item["family"]]
    finally:
        cur.close()
    swatches = [p for p in family if p["size_label"] == item["size_label"]]
    sizes = [p for p in family if p["style_code"] == item["style_code"]]
    fit = face_fit_for(sizes)
    lenses = [lens for lens in LENSES if item["rx_allowed"] or not lens["rx"]]
    return render_template("ai_glasses/product.html", p=item, swatches=swatches, sizes=sizes,
                           lenses=lenses, robots=_page_robots(), preview=not is_live(), fit=fit,
                           rx_limits={"sph": RX_SPH, "cyl": RX_CYL, "add": RX_ADD, "pd": RX_PD,
                                      "total": (float(item["rx_total_min"] or RX_SPH[0]),
                                                float(item["rx_total_max"] or RX_SPH[1]))})


def face_fit_for(sizes):
    """Optiwar's face-fit verdict (face_fit.evaluate) for each size of this style, for the
    person the signed-in customer is shopping for. Same engine and rule as the frame pages;
    the verdict is sealed on the order line at checkout (face_cart.record)."""
    if not session.get("user_id"):
        return {"state": "signed_out"}
    try:
        from . import face_fit, face_profiles, face_profiles_api
        db = _db()
        uid = session["user_id"]
        person, measurement = None, None
        if face_profiles_api.gate_enabled():
            face_profiles.ensure_schema(db)
            face_profiles.migrate_customer(db, uid, session.get("user_name"))
            row = face_fit.active_profile(db, uid, session)
            if row:
                person = row.get("display_name")
                measurement = face_fit.profile_measurement(row)
        else:
            cur = db.cursor()
            cur.execute("SELECT pd_far, pd_near, face_width, recommended_diameter, recommended_bridge, "
                        "recommended_length FROM face_measurements WHERE customer_id = %s "
                        "ORDER BY measured_at DESC LIMIT 1", (uid,))
            measurement = cur.fetchone()
            cur.close()
        if not measurement or not measurement.get("pd_far") or not measurement.get("face_width"):
            return {"state": "no_scan"}
        verdicts = {}
        for p in sizes:
            v = face_fit.evaluate(measurement, p["size_spec"])
            verdicts[p["product_id"]] = {"label": v["label"], "matched": v["matched"],
                                         "reasons": v["reasons"]}
        return {"state": "checked", "person": person, "sizes": verdicts}
    except Exception:  # noqa: BLE001 - a fit problem must never break the product page
        current_app.logger.exception("AI Glasses: face fit unavailable")
        return {"state": "unavailable"}


@bp.route(PAGE_PATH + "/add", methods=["POST"])
def add_to_bag():
    """Add one AI Glasses product to the cart. Everything is priced on the server."""
    _guard()
    db = _db()
    cur = db.cursor()
    try:
        cur.execute(_SELECT + " AND p.product_id = %s", (VERTICAL, int(request.form.get("product_id") or 0)))
        row = cur.fetchone()
        if not row:
            abort(404)
        p = _decorate(row)
        back = url_for("ai_glasses.product", slug=p["product_slug"])
        if not p["dispatch"]["can_buy"]:
            flash("This model is sold out.")
            return redirect(back)
        lens = LENS_BY_KEY.get(request.form.get("lens") or "none")
        if not lens or (lens["rx"] and not p["rx_allowed"]):
            flash("Please choose a lens option.")
            return redirect(back)

        rx_id, right_eye, left_eye = None, "No RX selected", "No RX selected"
        note = "AI Glasses: %s" % lens["label"]
        if lens["rx"]:
            if request.form.get("rx_mode") == "later":
                right_eye = left_eye = "To be sent by customer after ordering"
                note += "; prescription to be sent after ordering"
            else:
                rx, errors = validate_rx(request.form, p, lens)
                if errors:
                    for e in errors:
                        flash(e)
                    return redirect(back)
                right_eye, left_eye = _eye(rx["right"]), _eye(rx["left"])
                note += "; PD %s" % rx["pd"]
            cur.execute("INSERT INTO rx_collector (right_eye, left_eye, product_id, recommendations) "
                        "VALUES (%s, %s, %s, %s)", (right_eye, left_eye, p["product_id"], note))
            rx_id = cur.lastrowid
            db.commit()
            session.setdefault("rx_ids", []).append(rx_id)

        line = {
            "product_id": str(p["product_id"]), "product_name": p["product_name"],
            "product_code": p["product_code"], "product_category": "AI Glasses",
            "product_special_price": p["price"], "product_price": p["mrp"],
            "order_quantity": 1, "rx_id": rx_id, "right_eye": right_eye, "left_eye": left_eye,
            "lens_price": lens["price"], "cyl_price_increase": 0, "add_price_increase": 0,
            "addon_1_price": 0, "addon_2_price": 0, "addon_3_price": 0,
            "server_total_price": p["price"] + lens["price"], "recommendations": note,
            "lens_mrp": 0, "full_product_price": p["mrp"] + lens["price"],
            "total_savings": p["mrp"] - p["price"],
            "ai_glasses": 1, "aig_lens": lens["key"], "aig_lens_label": lens["label"],
        }
        cart = [i for i in session.get("cart", []) if not (
            str(i.get("product_id")) == line["product_id"] and i.get("aig_lens") == lens["key"] and not rx_id)]
        cart.append(line)
        session["cart"] = cart
        session.modified = True
        if session.get("user_id"):
            try:
                from .cart_persist import save_cart_to_db
                save_cart_to_db()
            except Exception:
                current_app.logger.exception("AI Glasses: cart sync failed")
        current_app.logger.info("[%s] ACTIVITY:ADD_TO_CART AI_GLASSES product:%s lens:%s price:%s rx_id:%s"
                                % (request.host, p["product_id"], lens["key"], line["server_total_price"], rx_id))
        return redirect(url_for("main.checkout"))
    finally:
        cur.close()


# ---- owner preview ------------------------------------------------------------------

def revalidate(cursor, cart):
    """Reprice every AI Glasses line from the database, and remove one this
    request may not buy: the other storefront or the flag off, sold out, or a
    line that did not come through add_to_bag (the older cart routes take their
    prices from the form). Returns (cart, names of the removed lines)."""
    ids = sorted({str(i.get("product_id")) for i in cart if str(i.get("product_id") or "").isdigit()})
    if not ids:
        return cart, []
    marks = ",".join(["%s"] * len(ids))
    cursor.execute("SELECT product_id FROM products WHERE product_vertical = %%s AND product_id IN (%s)" % marks,
                   [VERTICAL] + ids)
    ours = {str(r["product_id"]) for r in cursor.fetchall()}
    if not ours:
        return cart, []
    rows = {}
    if is_visible():
        cursor.execute(_SELECT + " AND p.product_id IN (%s)" % marks, [VERTICAL] + ids)
        rows = {str(r["product_id"]): r for r in cursor.fetchall()}
    kept, removed = [], []
    for item in cart:
        pid = str(item.get("product_id"))
        if pid not in ours:
            kept.append(item)
            continue
        row = rows.get(pid)
        lens = LENS_BY_KEY.get(item.get("aig_lens"))
        if (not row or not item.get("ai_glasses") or not lens
                or (lens["rx"] and not row["rx_allowed"]) or not _dispatch(row)["can_buy"]):
            removed.append(item.get("product_name") or "AI Glasses")
            continue
        price = _price(row)
        try:
            qty = max(1, int(item.get("order_quantity") or 1))
        except (TypeError, ValueError):
            qty = 1
        item.update(order_quantity=qty, product_special_price=price["price"], product_price=price["mrp"],
                    lens_price=lens["price"], cyl_price_increase=0, add_price_increase=0,
                    addon_1_price=0, addon_2_price=0, addon_3_price=0,
                    server_total_price=(price["price"] + lens["price"]) * qty,
                    full_product_price=price["mrp"] + lens["price"],
                    total_savings=price["mrp"] - price["price"])
        item.pop("ATC_total", None)
        item.pop("ATC_WCL", None)
        kept.append(item)
    return kept, removed


def describe_removed(names):
    return "Removed from your bag, as it can't be bought here right now: %s." % ", ".join(names)


@bp.route(PAGE_PATH + "/preview/<token>")
def preview(token):
    from itsdangerous import BadSignature, SignatureExpired
    if not _on_india_site():
        abort(404)
    try:
        _serializer().loads(token, max_age=PREVIEW_DAYS * 86400)
    except (BadSignature, SignatureExpired):
        abort(404)
    session["aig_preview_until"] = (datetime.datetime.utcnow() + datetime.timedelta(days=1)).timestamp()
    return redirect(PAGE_PATH)


# ---- Ops ------------------------------------------------------------------------------

def _ops_ok():
    from .ops import _require_ops_auth
    return _require_ops_auth()


def _ops_user():
    return (request.headers.get("X-Ops-User") or session.get("user_email") or "").strip()[:100]


@bp.route("/ops/api/ai-glasses/preview-link", methods=["POST"])
def ops_preview_link():
    if not _ops_ok():
        return jsonify({"error": "Unauthorized"}), 401
    token = _serializer().dumps({"by": _ops_user() or "ops"})
    return jsonify({"url": "https://optiwar.in%s/preview/%s" % (PAGE_PATH, token),
                    "expires_in_days": PREVIEW_DAYS})


def sold_counts(cursor):
    """Units sold per product: fulfilled (paid) lines, excluding test orders."""
    cursor.execute("SELECT o.product_id, SUM(o.order_quantity) AS sold FROM orders o "
                   "JOIN ai_glasses_products a ON a.product_id = o.product_id "
                   "WHERE o.fulfillment_status = 'fulfilled' AND o.is_test = 0 AND o.archived = 0 "
                   "GROUP BY o.product_id")
    return {int(r["product_id"]): int(r["sold"] or 0) for r in cursor.fetchall()}


@bp.route("/ops/api/ai-glasses", methods=["GET"])
def ops_list():
    if not _ops_ok():
        return jsonify({"error": "Unauthorized"}), 401
    cur = _db().cursor()
    try:
        sold = sold_counts(cur)
        cur.execute("SELECT p.product_id, p.product_code, p.product_name, p.product_price, "
                    "p.product_special_price, p.product_quantity, a.mpn, a.gtin, a.size_label, "
                    "a.sales_status, a.updated_at, a.updated_by FROM products p "
                    "JOIN ai_glasses_products a ON a.product_id = p.product_id ORDER BY a.family, a.sort_order")
        rows = cur.fetchall()
    finally:
        cur.close()
    for r in rows:
        r["sold"] = sold.get(int(r["product_id"]), 0)
        r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
    return jsonify({"live": is_live(), "statuses": SALES_STATUSES, "products": rows})


@bp.route("/ops/api/ai-glasses/<int:product_id>/status", methods=["POST"])
def ops_set_status(product_id):
    if not _ops_ok():
        return jsonify({"error": "Unauthorized"}), 401
    who = _ops_user()
    status = ((request.get_json(silent=True) or {}).get("status") or "").strip().upper()
    if not who:
        return jsonify({"error": "X-Ops-User header required"}), 400
    if status not in SALES_STATUSES:
        return jsonify({"error": "status must be one of %s" % ", ".join(SALES_STATUSES)}), 400
    db = _db()
    cur = db.cursor()
    try:
        cur.execute("SELECT sales_status FROM ai_glasses_products WHERE product_id = %s", (product_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "not an AI Glasses product"}), 404
        now = datetime.datetime.now()
        cur.execute("UPDATE ai_glasses_products SET sales_status = %s, updated_at = %s, updated_by = %s "
                    "WHERE product_id = %s", (status, now, who, product_id))
        cur.execute("INSERT INTO ai_glasses_status_log (product_id, old_status, new_status, changed_by, changed_at) "
                    "VALUES (%s, %s, %s, %s, %s)", (product_id, row["sales_status"], status, who, now))
        db.commit()
    finally:
        cur.close()
    return jsonify({"product_id": product_id, "old": row["sales_status"], "new": status, "by": who})


# ---- AI chat ---------------------------------------------------------------------------

def prompt_section(cursor, is_india):
    """The AI GLASSES block for the chat prompts (chat.py and chat_gateway.py).

    Empty unless AI Glasses are visible to this visitor (optiwar.in, live or the
    owner's preview), so the assistant can never mention them before launch.
    Built from the catalogue, so prices and statuses are always current.
    """
    try:
        if not is_india or not (is_live() or preview_active()):
            return ""
        products = load_products(cursor)
    except Exception:  # noqa: BLE001
        current_app.logger.exception("AI Glasses: chat section unavailable")
        return ""
    if not products:
        return ""
    lines = []
    for p in products:
        lines.append("  - %s | %s %s | %s | price %s%s | %s | %s" % (
            p["product_name"], p["size_label"], p["size_spec"], p["mpn"], money(p["price"]),
            (" (MRP %s, %s%% off)" % (money(p["mrp"]), p["off"])) if p["off"] else "",
            p["dispatch"]["text"], "https://optiwar.in" + p["url"]))
    lenses = "; ".join("%s %s" % (l["label"], ("+" + money(l["price"])) if l["price"] else "(included)") for l in LENSES)
    return """
AI GLASSES (a separate product line, sold on optiwar.in only):
Ray-Ban Meta Wayfarer (Gen 2): 5 styles x 2 sizes (Standard 50-22-150, Large 53-22-155). Page: https://optiwar.in%s
%s
- Facts you may state: 12 MP ultra-wide camera, photos and up to 3K video, open-ear speakers, built-in Meta AI ("Hey Meta"), up to 8 hours battery with charging case, pairs with the Meta AI app (Android/iOS). Do not state any other specification.
- Lenses: %s. AI Glasses do NOT come with Optiwar's free lenses or lens upgrades; never offer those for AI Glasses, and never apply AI Glasses lens prices to Optiwar frames.
- Prescription: only on models marked Rx ready, within the manufacturer's range: total power (SPH + CYL) %+.2f to %+.2f per eye. Outside that range, suggest contacting support. The customer can enter the prescription on the product page or send it after ordering.
- Dispatch follows each product's status above (pre-order: within %s days). Payment online only. Returns only if dead on arrival (DOA). Prices include GST.
- For AI Glasses, share the product or page link above instead of a [PRODUCTS:] tag.
""" % (PAGE_PATH, "\n".join(lines), lenses, RX_SPH[0], RX_SPH[1], PREORDER_DAYS)
