"""Checkout prescription photos with Ops-only readings and audited access."""

import datetime
import hashlib
import hmac
import json
import os
import re
import time

from flask import (current_app, jsonify, make_response, render_template, request,
                   send_file, session, url_for)

try:
    from . import (ai_client, customer_orders, dev_defects, lens_documents,
                   lens_rx, rx_lookup)
except ImportError:
    import ai_client
    import customer_orders
    import dev_defects
    import lens_documents
    import lens_rx
    import rx_lookup


def get_db():
    try:
        from .db import get_db as getter
    except ImportError:
        from db import get_db as getter
    return getter()


ST_UPLOADED = "UPLOADED"
ST_PARSED = "PARSED"
ST_UNREADABLE = "UNREADABLE"
ST_REMOVED = "REMOVED"
ST_PURGED = "PURGED"
LIVE_STATUSES = (ST_UPLOADED, ST_PARSED, ST_UNREADABLE)
LINK_TTL_SECONDS = 600

DOCUMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_rx_documents (
    document_id INT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    customer_id INT NOT NULL,
    product_id INT NOT NULL,
    site VARCHAR(32) NULL,
    status VARCHAR(16) NOT NULL,
    stored_name VARCHAR(80) NULL,
    original_kind VARCHAR(8) NOT NULL,
    original_bytes INT UNSIGNED NOT NULL,
    stored_bytes INT UNSIGNED NOT NULL DEFAULT 0,
    sha256 CHAR(64) NOT NULL,
    pages TINYINT UNSIGNED NOT NULL DEFAULT 1,
    provider VARCHAR(32) NULL,
    model VARCHAR(64) NULL,
    parsed_json TEXT NULL,
    confidence DECIMAL(3,2) NULL,
    refusal VARCHAR(255) NULL,
    typed_json TEXT NULL,
    comparison VARCHAR(16) NULL,
    differences_json TEXT NULL,
    order_id VARCHAR(32) NULL,
    order_rx_id INT NULL,
    attached_at DATETIME NULL,
    review_verdict VARCHAR(32) NULL,
    review_note VARCHAR(500) NULL,
    reviewed_by VARCHAR(191) NULL,
    reviewed_at DATETIME NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'checkout',
    chat_attachment_id BIGINT UNSIGNED NULL,
    created_at DATETIME NOT NULL,
    parsed_at DATETIME NULL,
    retain_until DATE NOT NULL,
    purged_at DATETIME NULL,
    KEY ix_ordrx_customer (customer_id, created_at),
    KEY ix_ordrx_order (order_id),
    KEY ix_ordrx_status (status, created_at),
    KEY ix_ordrx_retain (retain_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_rx_document_audit (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    document_id INT UNSIGNED NOT NULL,
    action VARCHAR(32) NOT NULL,
    actor VARCHAR(191) NULL,
    ip VARCHAR(64) NULL,
    detail VARCHAR(255) NULL,
    occurred_at DATETIME NOT NULL,
    KEY ix_ordrxa_doc (document_id, occurred_at),
    KEY ix_ordrxa_action (action, occurred_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

TABLES = (("order_rx_documents", DOCUMENT_SCHEMA),
          ("order_rx_document_audit", AUDIT_SCHEMA))

ADDED_COLUMNS = (("order_rx_documents", (
    ("source", "VARCHAR(16) NOT NULL DEFAULT 'checkout'"),
    ("chat_attachment_id", "BIGINT UNSIGNED NULL"),
)),)

_SCHEMA_READY = False


def ensure_schema(cursor):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    for _name, ddl in TABLES:
        cursor.execute(ddl)
    for table, columns in ADDED_COLUMNS:
        for name, declaration in columns:
            cursor.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s %s" %
                           (table, name, declaration))
    _SCHEMA_READY = True


def upload_dir():
    path = os.path.join(os.path.dirname(current_app.root_path),
                        "secure_uploads", "order_rx")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def stored_name(document_id, page=0):
    return "ordrx-%d-%d.jpg" % (int(document_id), int(page))


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else
            request.remote_addr or "")[:64]


def _ops_auth():
    try:
        from .ops import _require_ops_auth
    except ImportError:
        from ops import _require_ops_auth
    return _require_ops_auth()


def _ops_actor():
    user_email = session.get("user_email")
    if user_email:
        return user_email
    if request.headers.get("Authorization", "").startswith("Bearer "):
        ops_user = request.headers.get("X-Ops-User", "")
        return ("bearer:" + ops_user
                if re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", ops_user)
                else "bearer")
    return "unknown"


def _customer_id():
    try:
        return int(session.get("user_id"))
    except (TypeError, ValueError):
        return None


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _json(data, status=200):
    response = make_response(jsonify(data), status)
    return _no_store(response)


def _parse_json(value, fallback=None):
    if not value:
        return fallback
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return fallback


def typed_snapshot(item):
    return {key: item.get(key) for eye in ("right", "left")
            for key in (eye + "_pwr", eye + "_cyl", eye + "_axis",
                        eye + "_add")}


def preparing_rx_lines(cursor, customer_id, site_from=None):
    """Return this customer's paid, still-preparing spectacle order lines."""
    sql = (
        "SELECT o.order_id,o.order_line_id,o.product_id,o.rx_id,p.product_name,"
        "rc.right_eye,rc.left_eye,"
        "(SELECT os.order_status_name FROM order_status os "
        " WHERE os.order_id=o.order_id ORDER BY os.order_status_id DESC LIMIT 1) "
        "AS order_status_name "
        "FROM orders o JOIN products p ON p.product_id=o.product_id "
        "JOIN rx_collector rc ON rc.rx_id=o.rx_id "
        "WHERE o.customer_id=%%s AND o.is_test=0 AND o.archived=0 "
        "AND o.date_created >= NOW() - INTERVAL %d DAY "
        "AND EXISTS (SELECT 1 FROM payment_collector pc "
        " WHERE pc.order_id=o.order_id AND pc.status='TXN_SUCCESS') "
    ) % int(rx_lookup.ORDER_LOOKBACK_DAYS)
    params = [int(customer_id)]
    if site_from:
        sql += "AND o.site_from=%%s "
        params.append(site_from)
    sql += "ORDER BY o.date_created DESC,o.order_line_id DESC"
    cursor.execute(sql, tuple(params))
    result = []
    for row in cursor.fetchall():
        if customer_orders.stage(row.get("order_status_name"))[2] != 1:
            continue
        right = rx_lookup.parse_eye(row.get("right_eye"))
        left = rx_lookup.parse_eye(row.get("left_eye"))
        if right is None and left is None:
            continue
        typed = {}
        for side, eye in (("right", right), ("left", left)):
            typed[side + "_pwr"] = eye.get("sph") if eye else None
            typed[side + "_cyl"] = eye.get("cyl") if eye else None
            typed[side + "_axis"] = eye.get("axis") if eye else None
            typed[side + "_add"] = eye.get("add") if eye else None
        result.append({
            "order_id": str(row.get("order_id") or ""),
            "order_line_id": int(row["order_line_id"]),
            "product_id": int(row["product_id"]),
            "rx_id": int(row["rx_id"]),
            "product_name": row.get("product_name") or "",
            "typed": typed,
        })
    return result


def _number(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _same(a, b):
    return a is not None and b is not None and abs(a - b) <= 0.01


def _zero_equal(a, b):
    return (a is None or abs(a) <= 0.01) and (b is None or abs(b) <= 0.01)


def _axis(value):
    number = _number(value)
    return None if number is None else int(round(number)) % 180


def _display(value):
    number = _number(value)
    if number is None:
        return "—"
    return "%+.2f" % number


def compare_prescription(proposal, typed):
    """Compare a vision proposal to the customer's independently typed Rx."""
    proposal = lens_rx.proposal_from_mapping(proposal)
    if not isinstance(proposal, dict) or not any(proposal.get(e) for e in
                                                  ("right", "left")):
        return "NO_READING", []

    differences = []
    exact = True
    transposed = True
    for eye, label in (("right", "R"), ("left", "L")):
        typed_eye = {
            "sph": typed.get(eye + "_pwr"),
            "cyl": typed.get(eye + "_cyl"),
            "axis": typed.get(eye + "_axis"),
            "add": typed.get(eye + "_add"),
        }
        photo_eye = proposal.get(eye)
        has_typed_values = any(
            _number(typed_eye[key]) is not None for key in ("sph", "cyl", "add"))
        if not has_typed_values:
            if (isinstance(photo_eye, dict) and any(
                    _number(photo_eye.get(key)) is not None
                    for key in ("sph", "cyl", "add", "add_power"))):
                exact = transposed = False
                differences.append("%s: no typed value on the order" % label)
            continue
        if not isinstance(photo_eye, dict) or not photo_eye:
            exact = transposed = False
            differences.append("%s: not readable on photo" % label)
            continue

        photo = {key: _number(photo_eye.get(key)) for key in
                 ("sph", "cyl", "axis")}
        photo["add"] = _number(photo_eye.get("add", photo_eye.get("add_power")))
        typed_values = {key: _number(value) for key, value in typed_eye.items()}

        def values_match(candidate):
            for key in ("sph", "cyl", "add"):
                a, b = candidate.get(key), typed_values[key]
                if key in ("cyl", "add") and _zero_equal(a, b):
                    continue
                if not _same(a, b):
                    return False
            if not (_zero_equal(candidate.get("cyl"), None) or
                    _zero_equal(typed_values["cyl"], None)):
                if _axis(candidate.get("axis")) != _axis(typed_values["axis"]):
                    return False
            return True

        eye_exact = values_match(photo)
        transposed_photo = dict(photo)
        if photo["sph"] is not None and photo["cyl"] is not None:
            transposed_photo["sph"] = photo["sph"] + photo["cyl"]
            transposed_photo["cyl"] = -photo["cyl"]
            transposed_photo["axis"] = (
                None if photo["axis"] is None else (photo["axis"] + 90) % 180)
        eye_transposed = values_match(transposed_photo)
        exact = exact and eye_exact
        transposed = transposed and (eye_exact or eye_transposed)
        if not eye_exact and eye_transposed:
            differences.append(
                "%s: same prescription written in the other cylinder form" % label)
        elif not eye_exact:
            for field, title in (("sph", "SPH"), ("cyl", "CYL"),
                                 ("axis", "AXIS"), ("add", "ADD")):
                a, b = photo[field], typed_values[field]
                if field in ("cyl", "add") and _zero_equal(a, b):
                    continue
                if field == "axis" and (_zero_equal(photo["cyl"], None) or
                                         _zero_equal(typed_values["cyl"], None)):
                    continue
                equal = (_axis(a) == _axis(b) if field == "axis"
                         else _same(a, b))
                if not equal:
                    differences.append("%s %s: photo %s, typed %s" %
                                       (label, title, _display(a), _display(b)))

    if exact:
        return "MATCH", []
    if transposed:
        return "MATCH_TRANSPOSED", differences
    return "DIFFERENT", differences


def _comparison_from_json(parsed_json, typed):
    payload = _parse_json(parsed_json, {}) or {}
    return compare_prescription(payload.get("eyes"), typed)


def _audit(cursor, document_id, action, actor=None, ip=None, detail=None,
           now=None):
    cursor.execute(
        "INSERT INTO order_rx_document_audit "
        "(document_id,action,actor,ip,detail,occurred_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (int(document_id), action, actor, ip,
         str(detail)[:255] if detail is not None else None,
         now or datetime.datetime.now()))


def _write_pages(document_id, pages):
    directory = upload_dir()
    names = []
    total = 0
    try:
        for index, data in enumerate(pages):
            name = stored_name(document_id, index)
            path = os.path.join(directory, name)
            with open(path, "xb") as image:
                image.write(data)
            os.chmod(path, 0o600)
            names.append(name)
            total += len(data)
    except Exception:
        for name in names:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass
        raise
    return names[0], total


def _delete_files(document_id, pages):
    for page in range(max(1, int(pages or 1))):
        try:
            os.remove(os.path.join(upload_dir(), stored_name(document_id, page)))
        except FileNotFoundError:
            pass


def _provider_name():
    configured = (current_app.config.get("ORDER_RX_VISION_PROVIDER") or
                  os.environ.get("ORDER_RX_VISION_PROVIDER"))
    return lens_documents.provider_name(configured)


def _date_after_months(now, months):
    return lens_rx.retain_until(now)


def _cart_item_for_id(cart, document_id):
    wanted = str(document_id)
    for index, item in enumerate(cart):
        if wanted in {str(value) for value in item.get("rx_document_ids", [])}:
            return index, item
    return None, None


def checkout_documents(cursor, customer_id, cart):
    """Per-cart-line display metadata; never exposes an AI reading."""
    ensure_schema(cursor)
    output = {}
    for index, item in enumerate(cart):
        ids = list(dict.fromkeys(str(v) for v in
                                 item.get("rx_document_ids", []) if str(v).isdigit()))
        if not ids:
            output[index] = []
            continue
        placeholders = ",".join(["%s"] * len(ids))
        cursor.execute(
            "SELECT document_id,created_at,pages FROM order_rx_documents "
            "WHERE customer_id=%s AND product_id=%s AND document_id IN (" +
            placeholders + ") "
            "AND status IN ('UPLOADED','PARSED','UNREADABLE') "
            "AND order_id IS NULL AND purged_at IS NULL "
            "ORDER BY created_at DESC",
            tuple([int(customer_id), int(item["product_id"])] +
                  [int(v) for v in ids]))
        output[index] = [
            {"document_id": int(row["document_id"]),
             "created_at": row["created_at"].isoformat()
             if isinstance(row.get("created_at"), datetime.datetime)
             else row.get("created_at"),
             "pages": int(row.get("pages") or 1)}
            for row in cursor.fetchall()
        ]
    return output


def attach_for_order(cursor, order_id, customer_id, cart):
    """Attach only this customer's live, unattached cart documents."""
    savepoint = False
    try:
        cursor.execute("SAVEPOINT order_rx_attach")
        savepoint = True
        now = datetime.datetime.now()
        retain_until = _date_after_months(now, lens_rx.RETENTION_MONTHS)
        for item in cart:
            if not item.get("rx_document_ids") or not item.get("product_id"):
                continue
            typed = typed_snapshot(item)
            for raw_id in set(str(v) for v in item["rx_document_ids"]):
                if not raw_id.isdigit():
                    continue
                document_id = int(raw_id)
                cursor.execute(
                    "SELECT parsed_json FROM order_rx_documents WHERE "
                    "document_id=%s AND customer_id=%s AND product_id=%s "
                    "AND order_id IS NULL AND status IN "
                    "('UPLOADED','PARSED','UNREADABLE')",
                    (document_id, int(customer_id), int(item["product_id"])))
                row = cursor.fetchone()
                if not row:
                    continue
                comparison, differences = _comparison_from_json(
                    row.get("parsed_json"), typed)
                cursor.execute(
                    "UPDATE order_rx_documents SET order_id=%s,order_rx_id=%s,"
                    "attached_at=%s,retain_until=%s,typed_json=%s,"
                    "comparison=%s,differences_json=%s WHERE document_id=%s "
                    "AND customer_id=%s AND product_id=%s AND order_id IS NULL "
                    "AND status IN ('UPLOADED','PARSED','UNREADABLE')",
                    (str(order_id), item.get("rx_id"), now, retain_until,
                     json.dumps(typed, sort_keys=True), comparison,
                     json.dumps(differences, ensure_ascii=False),
                     document_id, int(customer_id), int(item["product_id"])))
                if cursor.rowcount:
                    _audit(cursor, document_id, "ATTACHED", actor="customer",
                           detail="order=%s" % order_id, now=now)
        if savepoint:
            cursor.execute("RELEASE SAVEPOINT order_rx_attach")
    except Exception as exc:
        if savepoint:
            try:
                cursor.execute("ROLLBACK TO SAVEPOINT order_rx_attach")
                cursor.execute("RELEASE SAVEPOINT order_rx_attach")
            except Exception:
                pass
        current_app.logger.error("RX document attachment failed: %s",
                                 type(exc).__name__)
        try:
            dev_defects.record("RX_DOCUMENT_ATTACH_FAILED",
                               where="order=%s %s" %
                               (str(order_id), type(exc).__name__))
        except Exception:
            pass
        return False
    return True


def attach_chat_photo(cursor, customer_id, attachment_id, line, data,
                      proposal, confidence, model):
    """Copy an already-read chat photo onto its customer's order line."""
    ensure_schema(cursor)
    order_id = str(line["order_id"])
    cursor.execute(
        "SELECT document_id FROM order_rx_documents WHERE "
        "chat_attachment_id=%s AND order_id=%s "
        "AND source='chat' AND status NOT IN ('REMOVED','PURGED') "
        "AND purged_at IS NULL ORDER BY document_id LIMIT 1",
        (int(attachment_id), order_id))
    existing = cursor.fetchone()
    if existing:
        return int(existing["document_id"])

    norm = lens_documents.normalise(data)
    now = datetime.datetime.now()
    typed = typed_snapshot(line.get("typed") or {})
    parsed_json = (lens_documents.parsed_payload(proposal, confidence)
                   if proposal else None)
    comparison, differences = compare_prescription(proposal, typed)
    status = ST_PARSED if parsed_json else ST_UNREADABLE
    document_id = None
    try:
        cursor.execute(
            "INSERT INTO order_rx_documents "
            "(customer_id,product_id,site,status,original_kind,original_bytes,"
            "stored_bytes,sha256,pages,provider,model,parsed_json,confidence,"
            "typed_json,comparison,differences_json,order_id,order_rx_id,"
            "attached_at,source,chat_attachment_id,created_at,parsed_at,"
            "retain_until) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (int(customer_id), int(line["product_id"]), request.host[:32],
             status, norm["kind"], norm["original_bytes"], norm["stored_bytes"],
             norm["sha256"], len(norm["pages"]), _provider_name(),
             (model or "")[:64] or None, parsed_json, confidence,
             json.dumps(typed, sort_keys=True), comparison,
             json.dumps(differences, ensure_ascii=False), order_id,
             int(line["rx_id"]), now, "chat", int(attachment_id), now, now,
             _date_after_months(now, lens_rx.RETENTION_MONTHS)))
        document_id = int(cursor.lastrowid)
        first_name, stored_bytes = _write_pages(document_id, norm["pages"])
        cursor.execute(
            "UPDATE order_rx_documents SET stored_name=%s,stored_bytes=%s "
            "WHERE document_id=%s",
            (first_name, stored_bytes, document_id))
        _audit(cursor, document_id, "UPLOADED", actor="customer",
               detail="chat attachment=%d" % int(attachment_id), now=now)
        _audit(cursor, document_id, "ATTACHED", actor="customer",
               detail="order=%s" % order_id, now=now)
        _audit(cursor, document_id,
               "PARSED" if parsed_json else "PARSE_FAILED",
               actor=_provider_name(),
               detail=("comparison=%s" % comparison) if parsed_json
               else "no readable proposal", now=now)
        cursor.connection.commit()
    except Exception:
        try:
            cursor.connection.rollback()
        except Exception:
            pass
        if document_id is not None:
            try:
                cursor.execute(
                    "DELETE FROM order_rx_document_audit WHERE document_id=%s",
                    (document_id,))
            finally:
                try:
                    cursor.execute(
                        "DELETE FROM order_rx_documents WHERE document_id=%s",
                        (document_id,))
                    cursor.connection.commit()
                finally:
                    _delete_files(document_id, len(norm["pages"]))
        raise
    return document_id


def _read_pages(document_id, pages):
    names = [stored_name(document_id, page) for page in range(int(pages or 1))]
    output = []
    for name in names:
        with open(os.path.join(upload_dir(), name), "rb") as image:
            output.append(image.read())
    return output


def _safe_order(order_id):
    value = str(order_id or "").strip().upper()
    return value[3:] if value.startswith("OW-") else value


def _sign(document_id, page, expires):
    secret = (current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    message = ("ordrx:%d:%d:%d" %
               (int(document_id), int(page), int(expires))).encode("ascii")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def signed_path(document_id, page):
    expires = int(time.time()) + LINK_TTL_SECONDS
    return url_for("main.order_rx_document_file",
                   document_id=int(document_id), page=int(page),
                   exp=expires, sig=_sign(document_id, page, expires))


def _valid_signature(document_id, page, expires, signature):
    try:
        expiration = int(expires)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    if expiration < now or expiration > now + LINK_TTL_SECONDS:
        return False
    return hmac.compare_digest(
        _sign(document_id, page, expiration), str(signature or ""))


def _datetime_text(value):
    return value.isoformat() if isinstance(value, (datetime.datetime,
                                                   datetime.date)) else value


def _document_view(row, cursor, issue_links=True):
    parsed = _parse_json(row.get("parsed_json"), {}) or {}
    typed = _parse_json(row.get("typed_json"), {}) or {}
    differences = _parse_json(row.get("differences_json"), []) or []
    pages = int(row.get("pages") or 1)
    links = []
    for page in range(pages):
        link = signed_path(row["document_id"], page) if issue_links else None
        links.append({"page": page, "view_url": link,
                      "download_url": link + "&download=1" if link else None})
        if issue_links:
            _audit(cursor, row["document_id"], "LINK_ISSUED",
                   actor=_ops_actor(), ip=_client_ip(),
                   detail="page=%d ttl=%d" % (page, LINK_TTL_SECONDS))
    return {
        "document_id": int(row["document_id"]),
        "customer_id": int(row["customer_id"]),
        "product_id": int(row["product_id"]),
        "product_name": row.get("product_name"),
        "order_id": row.get("order_id"),
        "order_rx_id": row.get("order_rx_id"),
        "status": row.get("status"),
        "source": row.get("source") or "checkout",
        "created_at": _datetime_text(row.get("created_at")),
        "attached_at": _datetime_text(row.get("attached_at")),
        "pages": pages,
        "stored_bytes": int(row.get("stored_bytes") or 0),
        "parsed": parsed.get("eyes"),
        "confidence": row.get("confidence"),
        "model": row.get("model"),
        "provider": row.get("provider"),
        "typed": typed,
        "comparison": row.get("comparison") or "NO_READING",
        "differences": differences,
        "review_verdict": row.get("review_verdict"),
        "review_note": row.get("review_note"),
        "reviewed_by": row.get("reviewed_by"),
        "reviewed_at": _datetime_text(row.get("reviewed_at")),
        "files": links,
    }


def _order_view(cursor, order_id, issue_links=True):
    order_id = _safe_order(order_id)
    cursor.execute(
        "SELECT o.order_line_id,o.product_id,o.rx_id,o.customer_id,"
        "p.product_name FROM orders o LEFT JOIN products p "
        "ON p.product_id=o.product_id WHERE o.order_id=%s "
        "ORDER BY o.order_line_id",
                   (order_id,))
    order_lines = list(cursor.fetchall())
    if not order_lines:
        return None
    cursor.execute(
        "SELECT d.*,p.product_name FROM order_rx_documents d "
        "LEFT JOIN products p ON p.product_id=d.product_id "
        "WHERE d.order_id=%s AND d.status NOT IN ('REMOVED','PURGED') "
        "AND d.purged_at IS NULL ORDER BY d.created_at DESC",
        (order_id,))
    documents = list(cursor.fetchall())
    lines = []
    for line in order_lines:
        line_docs = [row for row in documents
                     if (row.get("order_rx_id") is not None and
                         row.get("order_rx_id") == line.get("rx_id")) or
                     (row.get("order_rx_id") is None and
                      row.get("product_id") == line.get("product_id"))]
        lines.append({
            "order_line_id": line["order_line_id"],
            "product_id": line["product_id"],
            "product_name": line.get("product_name"),
            "documents": [_document_view(row, cursor, issue_links)
                          for row in line_docs],
        })
    return {"ok": True, "order_id": order_id, "lines": lines}


def _queue(cursor, issue_links=True):
    cursor.execute(
        "SELECT d.*,p.product_name FROM order_rx_documents d "
        "LEFT JOIN products p ON p.product_id=d.product_id "
        "WHERE d.order_id IS NOT NULL AND d.attached_at >= "
        "DATE_SUB(NOW(), INTERVAL 30 DAY) AND d.review_verdict IS NULL "
        "AND d.status NOT IN ('REMOVED','PURGED') AND d.purged_at IS NULL "
        "ORDER BY d.attached_at DESC")
    rows = list(cursor.fetchall())
    return [_document_view(row, cursor, issue_links) for row in rows]


def _vision_read(pages, endpoint):
    try:
        from . import lens_upload
    except ImportError:
        import lens_upload
    workload = _provider_name()
    proposal, confidence, model, unreadable = lens_upload.read_document(
        pages, workload, endpoint=endpoint)
    return workload, proposal, confidence, model, unreadable


def _json_response_for_document(ok, document_id, pages, stored_bytes, message,
                                status=200):
    return _json({"ok": ok, "document_id": int(document_id),
                  "pages": int(pages),
                  "stored_kb": round(int(stored_bytes) / 1024.0, 1),
                  "message": message}, status)


def register(bp):
    @bp.route("/checkout/rx-upload", methods=["POST"])
    def order_rx_upload():
        customer_id = _customer_id()
        if not customer_id:
            return _json({
                "ok": False,
                "error": "unauthorized",
                "message": "Please sign in again to add a photo.",
            }, 401)
        cart = session.get("cart", [])
        try:
            line_index = int(request.form.get("line", ""))
        except (TypeError, ValueError):
            line_index = -1
        if line_index < 0 or line_index >= len(cart):
            return _json({
                "ok": False,
                "code": "RX_UPLOAD_LINE",
                "message": "This frame has no prescription entered, so a "
                           "photo can't be added here.",
            }, 400)
        item = cart[line_index]
        if (str(request.form.get("product_id", "")) !=
                str(item.get("product_id", "")) or
                lens_rx.is_lens_line(item) or
                not any(item.get(key) not in (None, "") for key in
                        ("right_pwr", "left_pwr"))):
            return _json({
                "ok": False,
                "code": "RX_UPLOAD_LINE",
                "message": "This frame has no prescription entered, so a "
                           "photo can't be added here.",
            }, 400)
        upload = request.files.get("document")
        if upload is None:
            return _json({"ok": False, "code": "RX_UPLOAD_REJECTED",
                          "message": "Choose a photo or PDF first."}, 400)
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        ids = list(dict.fromkeys(str(v) for v in
                                 item.get("rx_document_ids", [])
                                 if str(v).isdigit()))
        live_count = 0
        if ids:
            placeholders = ",".join(["%s"] * len(ids))
            cursor.execute(
                "SELECT COUNT(*) AS n FROM order_rx_documents WHERE "
                "customer_id=%s AND product_id=%s AND document_id IN (" +
                placeholders + ") "
                "AND order_id IS NULL AND status NOT IN ('REMOVED','PURGED') "
                "AND purged_at IS NULL",
                tuple([customer_id, int(item["product_id"])] +
                      [int(v) for v in ids]))
            live_count = int(cursor.fetchone()["n"])
        if live_count >= 3:
            return _json({
                "ok": False,
                "code": "RX_UPLOAD_LINE_LIMIT",
                "message": "You can add up to 3 photos for this frame. "
                           "Remove one to add another.",
            }, 400)
        cursor.execute(
            "SELECT COUNT(*) AS n FROM order_rx_documents WHERE "
            "customer_id=%s AND created_at >= DATE_SUB(NOW(), INTERVAL 1 DAY)",
            (customer_id,))
        if int(cursor.fetchone()["n"]) >= 20:
            return _json({
                "ok": False,
                "code": "RX_UPLOAD_RATE_LIMIT",
                "message": "Too many uploads today. Please try again tomorrow "
                           "or send the photo in the chat.",
            }, 429)
        try:
            norm = lens_documents.normalise(
                upload.read(lens_documents.MAX_UPLOAD_BYTES + 1))
        except lens_documents.Rejected as exc:
            return _json({"ok": False, "code": exc.code,
                          "message": exc.message}, 400)

        now = datetime.datetime.now()
        cursor.execute(
            "INSERT INTO order_rx_documents "
            "(customer_id,product_id,site,status,original_kind,original_bytes,"
            "stored_bytes,sha256,pages,typed_json,created_at,retain_until) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (customer_id, int(item["product_id"]), request.host[:32],
             ST_UPLOADED, norm["kind"], norm["original_bytes"],
             norm["stored_bytes"], norm["sha256"], len(norm["pages"]),
             json.dumps(typed_snapshot(item), sort_keys=True), now,
             now.date() + datetime.timedelta(days=30)))
        document_id = int(cursor.lastrowid)
        try:
            first_name, stored_bytes = _write_pages(document_id, norm["pages"])
            cursor.execute(
                "UPDATE order_rx_documents SET stored_name=%s,stored_bytes=%s "
                "WHERE document_id=%s",
                (first_name, stored_bytes, document_id))
            _audit(cursor, document_id, "UPLOADED", actor="customer",
                   ip=_client_ip(), detail="%s pages=%d" %
                   (norm["kind"], len(norm["pages"])), now=now)
            db.commit()
        except Exception:
            db.rollback()
            _delete_files(document_id, len(norm["pages"]))
            current_app.logger.exception("Prescription photo storage failed")
            return _json({
                "ok": False,
                "code": "RX_UPLOAD_STORAGE_FAILED",
                "message": "We couldn't save that photo. Please try again.",
            }, 500)

        parse_failure_audited = False
        try:
            workload, proposal, confidence, model, unreadable = _vision_read(
                norm["pages"], "/checkout/rx-upload")
        except ai_client.ModelError as exc:
            cursor.execute(
                "UPDATE order_rx_documents SET status=%s,provider=%s,"
                "refusal=%s,parsed_at=%s WHERE document_id=%s",
                (ST_UNREADABLE, _provider_name(), type(exc).__name__,
                 datetime.datetime.now(), document_id))
            _audit(cursor, document_id, "PARSE_FAILED", actor=_provider_name(),
                   detail=type(exc).__name__)
            db.commit()
            parse_failure_audited = True
            unreadable = True
            proposal = None
            model = None
            confidence = None
            workload = _provider_name()

        parsed_json = (lens_documents.parsed_payload(proposal, confidence)
                       if proposal and not unreadable else None)
        status = ST_PARSED if parsed_json else ST_UNREADABLE
        comparison, differences = compare_prescription(
            proposal, typed_snapshot(item))
        if not (not unreadable and parsed_json):
            comparison, differences = "NO_READING", []
        cursor.execute(
            "UPDATE order_rx_documents SET status=%s,provider=%s,model=%s,"
            "parsed_json=%s,confidence=%s,comparison=%s,differences_json=%s,"
            "parsed_at=%s,refusal=IF(%s='UNREADABLE',COALESCE(refusal,'unreadable'),NULL) "
            "WHERE document_id=%s",
            (status, workload, model, parsed_json, confidence, comparison,
             json.dumps(differences, ensure_ascii=False),
             datetime.datetime.now(), status, document_id))
        if parsed_json:
            _audit(cursor, document_id, "PARSED", actor=workload,
                   detail="comparison=%s" % comparison)
        elif not parse_failure_audited:
            _audit(cursor, document_id, "PARSE_FAILED", actor=workload,
                   detail="unreadable")
        db.commit()
        item.setdefault("rx_document_ids", []).append(document_id)
        session["cart"] = cart
        session.modified = True
        try:
            from . import models
            models._persist_cart(cart)
        except ImportError:
            from models import _persist_cart
            _persist_cart(cart)
        message = (
            "We've kept your photo for our team, but couldn't read it clearly. "
            "A sharper, well-lit photo of the whole prescription helps — you "
            "can add another."
            if status == ST_UNREADABLE else
            "Prescription photo received. Our team will check it against the "
            "values you entered before your lenses are made.")
        return _json_response_for_document(
            True, document_id, len(norm["pages"]), stored_bytes, message)

    @bp.route("/checkout/rx-upload/<int:document_id>/remove", methods=["POST"])
    def order_rx_remove(document_id):
        customer_id = _customer_id()
        if not customer_id:
            return _json({
                "ok": False,
                "error": "unauthorized",
                "message": "Please sign in again to add a photo.",
            }, 401)
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        cursor.execute(
            "SELECT pages FROM order_rx_documents WHERE document_id=%s "
            "AND customer_id=%s AND order_id IS NULL AND purged_at IS NULL "
            "AND status IN ('UPLOADED','PARSED','UNREADABLE')",
            (document_id, customer_id))
        row = cursor.fetchone()
        if not row:
            return _json({
                "ok": False,
                "error": "not_found",
                "message": "That photo can't be removed. It may already be "
                           "attached or unavailable.",
            }, 404)
        _delete_files(document_id, row.get("pages"))
        cursor.execute(
            "UPDATE order_rx_documents SET status=%s WHERE document_id=%s "
            "AND customer_id=%s AND order_id IS NULL",
            (ST_REMOVED, document_id, customer_id))
        _audit(cursor, document_id, "REMOVED", actor="customer",
               ip=_client_ip())
        cart = session.get("cart", [])
        index, item = _cart_item_for_id(cart, document_id)
        if item is not None:
            item["rx_document_ids"] = [
                value for value in item.get("rx_document_ids", [])
                if str(value) != str(document_id)]
            session["cart"] = cart
            session.modified = True
            try:
                from . import models
                models._persist_cart(cart)
            except ImportError:
                from models import _persist_cart
                _persist_cart(cart)
        db.commit()
        return _json({"ok": True})

    @bp.route("/ops/api/orders/<order_id>/rx-documents", methods=["GET"])
    def order_rx_api(order_id):
        if not _ops_auth():
            return _json({"error": "Unauthorized"}, 401)
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        result = _order_view(
            cursor, order_id, issue_links=request.args.get("links") != "0")
        if result is None:
            return _json({"ok": False, "error": "not_found"}, 404)
        db.commit()
        return _json(result)

    @bp.route("/ops/api/rx-documents/<int:document_id>/links",
              methods=["GET"])
    def order_rx_document_links(document_id):
        if not _ops_auth():
            return _json({"error": "Unauthorized"}, 401)
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        cursor.execute(
            "SELECT d.*,p.product_name FROM order_rx_documents d "
            "LEFT JOIN products p ON p.product_id=d.product_id "
            "WHERE d.document_id=%s AND d.order_id IS NOT NULL "
            "AND d.status NOT IN ('REMOVED','PURGED') "
            "AND d.purged_at IS NULL",
            (document_id,))
        row = cursor.fetchone()
        if not row:
            return _json({"ok": False, "error": "not_found"}, 404)
        document = _document_view(row, cursor)
        db.commit()
        return _json({"ok": True, "document_id": int(document_id),
                      "files": document["files"]})

    @bp.route("/ops/rx-documents", methods=["GET"])
    def order_rx_page():
        if not _ops_auth():
            return _no_store(make_response("Unauthorized", 401))
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        order = request.args.get("order", "").strip()
        if order:
            result = _order_view(cursor, order)
            if result is None:
                response = make_response(
                    render_template("ops_order_rx_documents.html",
                                    order=order, result=None, queue=None),
                    404)
            else:
                response = make_response(
                    render_template("ops_order_rx_documents.html",
                                    order=order, result=result, queue=None))
        else:
            queue = _queue(cursor)
            response = make_response(
                render_template("ops_order_rx_documents.html",
                                order="", result=None, queue=queue))
        db.commit()
        return _no_store(response)

    @bp.route("/ops/rx-documents/<int:document_id>/file/<int:page>",
              methods=["GET"])
    def order_rx_document_file(document_id, page):
        if not _ops_auth():
            return _json({"error": "Unauthorized"}, 401)
        if not _valid_signature(document_id, page, request.args.get("exp"),
                                request.args.get("sig")):
            return _json({"error": "link_invalid_or_expired"}, 403)
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        cursor.execute(
            "SELECT order_id,pages,status,purged_at FROM order_rx_documents "
            "WHERE document_id=%s", (document_id,))
        row = cursor.fetchone()
        if (not row or not row.get("order_id") or row.get("purged_at") or
                row.get("status") in (ST_REMOVED, ST_PURGED) or
                page >= int(row.get("pages") or 1)):
            return _json({"error": "not_found"}, 404)
        path = os.path.join(upload_dir(), stored_name(document_id, page))
        if not os.path.isfile(path):
            return _json({"error": "not_found"}, 404)
        download = request.args.get("download") == "1"
        _audit(cursor, document_id,
               "DOWNLOADED" if download else "VIEWED",
               actor=_ops_actor(), ip=_client_ip(),
               detail="page=%d" % page)
        db.commit()
        response = send_file(path, mimetype="image/jpeg", max_age=0)
        safe_order = re.sub(r"[^A-Z0-9-]", "", str(row["order_id"]).upper())
        disposition = "attachment" if download else "inline"
        response.headers["Content-Disposition"] = (
            '%s; filename="OW-%s-rx-%d-p%d.jpg"' %
            (disposition, safe_order, document_id, page))
        response.headers["X-Content-Type-Options"] = "nosniff"
        return _no_store(response)

    @bp.route("/ops/api/rx-documents/<int:document_id>/review",
              methods=["POST"])
    def order_rx_review(document_id):
        if not _ops_auth():
            return _json({"error": "Unauthorized"}, 401)
        body = request.get_json(silent=True) or {}
        verdict = str(body.get("verdict") or "").strip().upper()
        note = str(body.get("note") or "").strip()
        allowed = ("MATCHES_ORDER", "DIFFERENT_CONTACT_CUSTOMER",
                   "UNREADABLE_ASK_CUSTOMER")
        if verdict not in allowed:
            return _json({"ok": False, "error": "invalid_verdict"}, 400)
        if verdict != "MATCHES_ORDER" and not note:
            return _json({"ok": False, "error": "note_required"}, 400)
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        cursor.execute(
            "SELECT order_id FROM order_rx_documents WHERE document_id=%s "
            "AND order_id IS NOT NULL AND status NOT IN ('REMOVED','PURGED') "
            "AND purged_at IS NULL", (document_id,))
        if not cursor.fetchone():
            return _json({"ok": False, "error": "not_found"}, 404)
        now = datetime.datetime.now()
        actor = _ops_actor()
        cursor.execute(
            "UPDATE order_rx_documents SET review_verdict=%s,review_note=%s,"
            "reviewed_by=%s,reviewed_at=%s WHERE document_id=%s",
            (verdict, note[:500] or None, actor, now, document_id))
        _audit(cursor, document_id, "REVIEWED", actor=actor, ip=_client_ip(),
               detail=verdict)
        db.commit()
        return _json({"ok": True, "verdict": verdict})

    @bp.route("/ops/api/rx-documents/<int:document_id>/reparse",
              methods=["POST"])
    def order_rx_reparse(document_id):
        if not _ops_auth():
            return _json({"error": "Unauthorized"}, 401)
        db = get_db()
        cursor = db.cursor()
        ensure_schema(cursor)
        cursor.execute(
            "SELECT pages,order_id,status FROM order_rx_documents "
            "WHERE document_id=%s AND order_id IS NOT NULL "
            "AND status NOT IN ('REMOVED','PURGED') AND purged_at IS NULL",
            (document_id,))
        row = cursor.fetchone()
        if not row:
            return _json({"ok": False, "error": "not_found"}, 404)
        try:
            pages = _read_pages(document_id, row["pages"])
            workload, proposal, confidence, model, unreadable = _vision_read(
                pages, "/ops/api/rx-documents/%d/reparse" % document_id)
        except ai_client.ModelError as exc:
            proposal, confidence, model = None, None, None
            workload, unreadable = _provider_name(), True
            refusal = type(exc).__name__
        except OSError:
            return _json({"ok": False, "error": "file_missing"}, 404)
        else:
            refusal = "unreadable" if unreadable or not proposal else None
        parsed = (lens_documents.parsed_payload(proposal, confidence)
                  if proposal and not unreadable else None)
        cursor.execute(
            "SELECT typed_json FROM order_rx_documents WHERE document_id=%s",
            (document_id,))
        typed = _parse_json(cursor.fetchone().get("typed_json"), {}) or {}
        comparison, differences = compare_prescription(proposal, typed)
        if not parsed:
            comparison, differences = "NO_READING", []
        cursor.execute(
            "UPDATE order_rx_documents SET status=%s,provider=%s,model=%s,"
            "parsed_json=%s,confidence=%s,refusal=%s,comparison=%s,"
            "differences_json=%s,parsed_at=%s WHERE document_id=%s",
            (ST_PARSED if parsed else ST_UNREADABLE, workload, model, parsed,
             confidence, refusal, comparison,
             json.dumps(differences, ensure_ascii=False),
             datetime.datetime.now(), document_id))
        _audit(cursor, document_id, "REPARSED", actor=_ops_actor(),
               ip=_client_ip(), detail="comparison=%s" % comparison)
        db.commit()
        return _json({"ok": True, "comparison": comparison})


def purge_expired(db, today=None, now=None):
    cursor = db.cursor()
    ensure_schema(cursor)
    today = today or datetime.date.today()
    now = now or datetime.datetime.now()
    cursor.execute(
        "SELECT document_id,pages FROM order_rx_documents WHERE "
        "status<>'PURGED' AND (status='REMOVED' OR retain_until<%s)",
        (today,))
    rows = list(cursor.fetchall())
    purged = []
    for row in rows:
        try:
            _delete_files(row["document_id"], row.get("pages"))
        except OSError as exc:
            dev_defects.record("RX_DOCUMENT_PURGE_FAILED",
                               where="document_id=%s %s" %
                               (row["document_id"], type(exc).__name__))
            continue
        cursor.execute(
            "UPDATE order_rx_documents SET status='PURGED',purged_at=%s "
            "WHERE document_id=%s AND status<>'PURGED'",
            (now, row["document_id"]))
        _audit(cursor, row["document_id"], "PURGED", actor="retention",
               detail="files removed", now=now)
        purged.append(int(row["document_id"]))
    db.commit()
    return purged
