"""The Ops lens import console: one product at a time, a human confirming.

    parse    -> a staging row with the deterministic validator's report
    review   -> the model's findings about that staging row; writes nothing
    preview  -> an owner-only signed link to the hidden product page
    confirm  -> one transaction, the shared writer, ``merchant_enabled=0``
    withdraw -> everything of the product marked unavailable; nothing deleted
    release  -> ``merchant_enabled=1`` only when the shared readiness gate passes
    log      -> the append-only trail of the six above

Everything that decides what a lens *is* lives elsewhere and is reused here:
``cl_import`` validates, ``lens_rules`` compiles a chart's tiers,
``image_pipeline`` describes the views, ``lens_import_write`` writes,
``lens_preview`` signs, ``catalogue.lens_release_blockers`` releases. This
module owns the staging record, the audit trail and the order of steps. The
model is a reviewer: its findings are stored beside the payload, never in it.
"""

import datetime
import hashlib
import json
import re

try:
    from . import (catalogue, cl_import, image_pipeline, lens_import_write,
                   lens_preview, lens_rules, lens_view)
    from .catalogue import SITE_COM
    from .lens_import_schema import TABLES  # noqa: F401 - the console's schema
except ImportError:  # run as a plain module (tests, scripts)
    import catalogue
    import cl_import
    import image_pipeline
    import lens_import_write
    import lens_preview
    import lens_rules
    import lens_view
    from catalogue import SITE_COM
    from lens_import_schema import TABLES  # noqa: F401

STAGED = "STAGED"
REJECTED = "REJECTED"
CONFIRMED = "CONFIRMED"
FAILED = "FAILED"

ACT_PARSE = "PARSE"
ACT_REVIEW = "REVIEW"
ACT_PREVIEW = "PREVIEW"
ACT_CONFIRM = "CONFIRM"
ACT_WITHDRAW = "WITHDRAW"
ACT_RELEASE = "RELEASE"

OK = "OK"
REFUSED = "REFUSED"

SOURCE_SYSTEM = "ops_console"
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
PREVIEW_HOURS = 72

# What the model may say about a staging row, and nothing else. A finding
# names a field and a reason; it is never a value for that field.
SEVERITIES = ("BLOCK", "WARN", "INFO")

REVIEW_PROMPT = """You are reviewing ONE contact-lens catalogue entry that a
human operator has prepared for import, together with the deterministic
validator's report. You do not fill, correct or invent any value. Point out,
as findings only, anything that looks wrong or unsupported: a base curve or
diameter unusual for the stated product, a power range wider than the
manufacturer publishes, a made-to-order tier that contradicts the availability
text, a lead time stated without its source, a GTIN stated without the power
it was read from, an alias that is really a different product, a pack quantity
that does not match the name, a brand/manufacturer mismatch, or missing
evidence. If carton photographs are attached, say whether the printed values
you can read agree with the entry.

Answer with JSON only:
{"findings": [{"severity": "BLOCK|WARN|INFO", "field": "<field or ''>",
               "message": "<one sentence>"}]}
An empty list means you found nothing to raise."""

_FENCE = re.compile(r"^```(?:json)?|```$", re.MULTILINE)


class ImportRefused(Exception):
    """A step that must not go ahead, with the reason the operator sees."""

    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _stamp(now=None):
    return (now or datetime.datetime.utcnow()).replace(microsecond=0)


def canonical_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def payload_sha256(payload):
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _actor(value):
    return (str(value).strip()[:80] or None) if value else None


def log(cursor, action, outcome, actor=None, staging_id=None,
        product_id=None, source_ref=None, detail=None, sha=None):
    cursor.execute(
        "INSERT INTO contact_lens_import_log (action, staging_id, product_id,"
        " source_ref, actor, outcome, detail_json, payload_sha256)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (action, staging_id, product_id, source_ref, _actor(actor), outcome,
         json.dumps(detail, sort_keys=True, default=str) if detail else None,
         sha))
    return cursor.lastrowid


def staging_row(cursor, staging_id):
    try:
        wanted = int(staging_id)
    except (TypeError, ValueError):
        return None
    cursor.execute("SELECT * FROM contact_lens_import_staging WHERE "
                   "staging_id=%s", (wanted,))
    row = cursor.fetchone()
    return dict(row) if row else None


def _json_field(row, key):
    text = row.get(key)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def staging_view(row):
    """What the console shows for a staging row: never the raw payload twice."""
    return {
        "staging_id": row["staging_id"],
        "source_ref": row["source_ref"],
        "status": row["status"],
        "payload_sha256": row["payload_sha256"],
        "product_id": row.get("product_id"),
        "report": _json_field(row, "report_json"),
        "review": _json_field(row, "review_json"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "confirmed_by": row.get("confirmed_by"),
        "confirmed_at": row.get("confirmed_at"),
    }


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def _shape(payload):
    """The payload's parts, or the reason it has no usable shape."""
    if not isinstance(payload, dict):
        raise ImportRefused("payload_shape", "payload must be a JSON object")
    product = payload.get("product")
    if not isinstance(product, dict) or not product:
        raise ImportRefused("payload_shape", "payload.product is required")
    parts = {
        "product": product,
        "variants": payload.get("variants") or [],
        "rules": payload.get("rules") or [],
        "rule_set": payload.get("rule_set"),
        "recipe": payload.get("recipe"),
        "images": payload.get("images") or [],
    }
    for key in ("variants", "rules", "images"):
        if not isinstance(parts[key], list) or not all(
                isinstance(r, dict) for r in parts[key]):
            raise ImportRefused("payload_shape",
                                "payload.%s must be a list of objects" % key)
    stated = [k for k in ("variants", "rules", "rule_set") if parts[k]]
    if len(stated) > 1:
        raise ImportRefused(
            "payload_shape",
            "state the matrix once: variants, rules or rule_set, not %s"
            % " and ".join(stated))
    if parts["rule_set"] is not None and not isinstance(parts["rule_set"], dict):
        raise ImportRefused("payload_shape", "payload.rule_set must be an object")
    if parts["recipe"] is not None and not isinstance(parts["recipe"], dict):
        raise ImportRefused("payload_shape", "payload.recipe must be an object")
    return parts


def _rule_set_rows(product_row, rule_set):
    """A chart's tiers compiled to the variant rows the validator checks.

    The compiler is the authority on what the tiers mean; the validator then
    sees the very rows a confirm would publish, so a rule set that compiles to
    something the product cannot carry is refused at parse, not at confirm.
    """
    compiled = lens_rules.compile_rules(rule_set)
    ref = product_row.get("source_ref")
    rows = []
    for combo in compiled["rows"]:
        row = {"source_ref": ref}
        for key in ("base_curve", "diameter", "color_code", "color_name",
                    "sph", "cyl", "axis", "add_power"):
            value = combo.get(key)
            if value is not None and value != "":
                row[key] = str(value)
        rows.append(row)
    return rows, compiled["counts"]


def validate(payload):
    """The deterministic report for one payload: ``(product_or_None, report)``.

    Pure: reads the payload, calls the validators, writes nothing.
    """
    parts = _shape(payload)
    product_row = dict(parts["product"])
    report = {"errors": [], "warnings": [], "matrix": None, "images": None}
    variant_rows = parts["variants"]
    if parts["rule_set"]:
        try:
            variant_rows, counts = _rule_set_rows(product_row, parts["rule_set"])
        except lens_rules.RuleError as exc:
            report["errors"].append({"sheet": "rule_set", "row": 0,
                                     "source_ref": product_row.get("source_ref"),
                                     "reason": str(exc)})
            return None, report
        report["matrix"] = dict(counts, mode="RULE_SET")
        product_row.setdefault("param_mode", cl_import.PARAM_MODE_MATRIX)
    products, errors = cl_import.parse([product_row], variant_rows,
                                       parts["rules"])
    report["errors"] += [
        {"sheet": sheet, "row": number, "source_ref": ref, "reason": why}
        for sheet, number, ref, why in errors]
    product = products[0] if products and not errors else None
    if product is not None and report["matrix"] is None:
        report["matrix"] = {
            "mode": product.get("param_mode"),
            "combinations": len(product["variants"]),
            "stated_values": len(product["rules"]),
        }
    if parts["recipe"] is not None:
        try:
            recipe = image_pipeline.validate_recipe(dict(parts["recipe"]))
            records = image_pipeline.image_records(recipe)
            qa = image_pipeline.qa_warnings(recipe, records)
        except image_pipeline.RecipeError as exc:
            report["errors"].append({"sheet": "recipe", "row": 0,
                                     "source_ref": product_row.get("source_ref"),
                                     "reason": str(exc)})
            product = None
        else:
            report["images"] = {
                "views": [{"code": r["code"], "view": r["view"],
                           "is_primary": r["is_primary"], "path": r["path"]}
                          for r in records],
                "primary": lens_import_write.primary_path(records),
            }
            for level, message in qa:
                (report["errors"] if level == "BLOCK"
                 else report["warnings"]).append(
                    {"sheet": "recipe", "row": 0,
                     "source_ref": product_row.get("source_ref"),
                     "reason": message})
            if any(w["sheet"] == "recipe" for w in report["errors"]):
                product = None
    elif parts["images"]:
        report["warnings"].append({
            "sheet": "images", "row": 0,
            "source_ref": product_row.get("source_ref"),
            "reason": "%d image note(s) recorded as evidence only; public views"
                      " come from an approved recipe" % len(parts["images"])})
    if product is not None:
        if product["gtin"] and not product["gtin_reference_power"]:
            report["warnings"].append({
                "sheet": "products", "row": 2,
                "source_ref": product["source_ref"],
                "reason": "gtin stated without gtin_reference_power; the feed"
                          " will not send it until the power is recorded"})
        report["identity"] = {
            "product_code": lens_import_write.product_code(product),
            "slug": lens_import_write.slug(product),
            "canonical_name": product["canonical_name"] or product["product_name"],
            "also_known_as": product["also_known_as"],
            "legacy_ref_id": product["legacy_ref_id"],
            "merchant_enabled": 0,
            "sell_on": dict(lens_import_write.SELL_ON),
        }
    report["ok"] = product is not None and not report["errors"]
    return (product if report["ok"] else None), report


def stage(cursor, payload, actor=None, now=None):
    """Validate and keep the payload as a staging row. Returns its view.

    A rejected payload is kept too, as REJECTED: the operator's attempt and
    the reasons are part of the record, and the confirm step refuses it.
    """
    if len(canonical_json(payload).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ImportRefused("payload_too_large", "payload exceeds %d bytes"
                            % MAX_PAYLOAD_BYTES)
    product, report = validate(payload)
    ref = cl_import._text(payload["product"].get("source_ref"))[:80]  # noqa: SLF001
    if not ref:
        raise ImportRefused("payload_shape", "payload.product.source_ref is required")
    sha = payload_sha256(payload)
    status = STAGED if report["ok"] else REJECTED
    cursor.execute(
        "INSERT INTO contact_lens_import_staging (source_system, source_ref,"
        " status, payload_json, payload_sha256, report_json, created_by,"
        " created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (SOURCE_SYSTEM, ref, status, canonical_json(payload), sha,
         json.dumps(report, sort_keys=True, default=str), _actor(actor),
         _stamp(now)))
    staging_id = cursor.lastrowid
    log(cursor, ACT_PARSE, OK if report["ok"] else REFUSED, actor,
        staging_id=staging_id, source_ref=ref, sha=sha,
        detail={"errors": len(report["errors"]),
                "warnings": len(report["warnings"])})
    return staging_view(staging_row(cursor, staging_id))


# ---------------------------------------------------------------------------
# review (findings only)
# ---------------------------------------------------------------------------

def review_messages(payload, report, photos_jpeg=()):
    """The question put to the model: the payload, the report, the photos."""
    import base64  # noqa: PLC0415
    content = [{"type": "text", "text": REVIEW_PROMPT},
               {"type": "text", "text": "ENTRY:\n" + json.dumps(
                   payload, indent=1, sort_keys=True, default=str)},
               {"type": "text", "text": "VALIDATOR REPORT:\n" + json.dumps(
                   report, indent=1, sort_keys=True, default=str)}]
    for photo in photos_jpeg:
        content.append({"type": "image_url", "image_url": {
            "url": "data:image/jpeg;base64,"
                   + base64.b64encode(photo).decode("ascii"),
            "detail": "high"}})
    return [{"role": "user", "content": content}]


def parse_findings(text):
    """The model's findings as a clean list; anything else is one INFO finding.

    Whatever the model returned that is not a finding — a corrected value, a
    rewritten entry — is dropped here, so the review cannot carry a value into
    the console even if the model volunteers one.
    """
    cleaned = _FENCE.sub("", (text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return [{"severity": "INFO", "field": "",
                 "message": "review returned no findings object"}]
    try:
        data = json.loads(cleaned[start:end + 1])
    except ValueError:
        return [{"severity": "INFO", "field": "",
                 "message": "review returned unreadable findings"}]
    findings = []
    for item in (data.get("findings") if isinstance(data, dict) else None) or ():
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "INFO").strip().upper()
        message = " ".join(str(item.get("message") or "").split())[:500]
        if not message:
            continue
        findings.append({
            "severity": severity if severity in SEVERITIES else "INFO",
            "field": str(item.get("field") or "").strip()[:60],
            "message": message,
        })
    return findings


def review(cursor, staging_id, ask, actor=None, photos_jpeg=(), now=None):
    """Store the model's findings beside the staging row. Returns them.

    ``ask(messages) -> text`` is the caller's model call. The payload,
    report and status of the staging row are left exactly as they were: a
    review changes what the operator knows, not what will be written.
    """
    row = staging_row(cursor, staging_id)
    if row is None:
        raise ImportRefused("not_found", "no staging row %s" % staging_id)
    payload = _json_field(row, "payload_json")
    report = _json_field(row, "report_json")
    text = ask(review_messages(payload, report, photos_jpeg))
    findings = parse_findings(text)
    result = {"findings": findings, "reviewed_at": _stamp(now).isoformat(),
              "payload_sha256": row["payload_sha256"], "authoritative": False}
    cursor.execute(
        "UPDATE contact_lens_import_staging SET review_json=%s, reviewed_at=%s"
        " WHERE staging_id=%s",
        (json.dumps(result, sort_keys=True), _stamp(now), row["staging_id"]))
    log(cursor, ACT_REVIEW, OK, actor, staging_id=row["staging_id"],
        source_ref=row["source_ref"], sha=row["payload_sha256"],
        detail={"findings": len(findings),
                "block": sum(1 for f in findings if f["severity"] == "BLOCK")})
    return result


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------

def _already_confirmed(cursor, source_ref, sha):
    cursor.execute(
        "SELECT staging_id, product_id FROM contact_lens_import_staging WHERE"
        " source_system=%s AND source_ref=%s AND payload_sha256=%s AND"
        " status=%s AND product_id IS NOT NULL ORDER BY staging_id DESC LIMIT 1",
        (SOURCE_SYSTEM, source_ref, sha, CONFIRMED))
    row = cursor.fetchone()
    return dict(row) if row else None


def confirm(db, staging_id, actor, rate=None, now=None):
    """Write the staged product, hidden, in one transaction. Returns a dict.

    The payload is re-validated here rather than trusted from the staging row,
    so the writer only ever sees what the validator passes now. Same
    ``source_ref`` and identical payload already confirmed: nothing is
    written again and the earlier product is returned. Same ``source_ref``
    with a changed payload: an update through the same idempotent upserts.
    Anything raising inside rolls the whole product back and the staging row
    stays STAGED with a FAILED log entry.
    """
    if not _actor(actor):
        raise ImportRefused("actor_required", "confirm names the human confirming")
    cursor = db.cursor()
    row = staging_row(cursor, staging_id)
    if row is None:
        raise ImportRefused("not_found", "no staging row %s" % staging_id)
    if row["status"] != STAGED:
        raise ImportRefused("not_stageable", "staging row %s is %s"
                            % (row["staging_id"], row["status"]),
                            {"status": row["status"]})
    payload = _json_field(row, "payload_json")
    product, report = validate(payload)
    if product is None:
        log(cursor, ACT_CONFIRM, REFUSED, actor, staging_id=row["staging_id"],
            source_ref=row["source_ref"], sha=row["payload_sha256"],
            detail={"errors": report["errors"]})
        db.commit()
        raise ImportRefused("validation_failed",
                            "the payload no longer validates", report)
    earlier = _already_confirmed(cursor, row["source_ref"], row["payload_sha256"])
    if earlier:
        cursor.execute(
            "UPDATE contact_lens_import_staging SET status=%s, product_id=%s,"
            " confirmed_by=%s, confirmed_at=%s WHERE staging_id=%s",
            (CONFIRMED, earlier["product_id"], _actor(actor), _stamp(now),
             row["staging_id"]))
        log(cursor, ACT_CONFIRM, OK, actor, staging_id=row["staging_id"],
            product_id=earlier["product_id"], source_ref=row["source_ref"],
            sha=row["payload_sha256"],
            detail={"already_confirmed_as": earlier["staging_id"], "writes": 0})
        db.commit()
        return {"product_id": earlier["product_id"], "created": False,
                "already_confirmed": True, "merchant_enabled": 0}
    parts = _shape(payload)
    records = None
    if parts["recipe"] is not None:
        records = image_pipeline.image_records(
            image_pipeline.validate_recipe(dict(parts["recipe"])))
    try:
        created = lens_import_write.existing(cursor, product) is None
        product_id, written, withdrawn = lens_import_write.import_one(
            cursor, product, rate, records,
            write_variants=parts["rule_set"] is None)
        published = None
        if parts["rule_set"] is not None:
            stored = lens_rules.store_rule_set(
                cursor, product_id, parts["rule_set"],
                source_type=parts["rule_set"].get("source_type"),
                source_ref=parts["rule_set"].get("source_ref"),
                source_date=parts["rule_set"].get("source_date"),
                confirmed_by=_actor(actor), now=now)
            lens_rules.compile_and_publish(cursor, product_id,
                                           stored["rule_version"], now=now)
            published = stored["rule_version"]
        cursor.execute(
            "UPDATE contact_lens_import_staging SET status=%s, product_id=%s,"
            " confirmed_by=%s, confirmed_at=%s WHERE staging_id=%s",
            (CONFIRMED, product_id, _actor(actor), _stamp(now),
             row["staging_id"]))
        log(cursor, ACT_CONFIRM, OK, actor, staging_id=row["staging_id"],
            product_id=product_id, source_ref=row["source_ref"],
            sha=row["payload_sha256"],
            detail={"created": created, "rule_version": published,
                    "rows_written": written, "rows_withdrawn": withdrawn,
                    "views": len(records or ()), "merchant_enabled": 0})
    except Exception as exc:
        db.rollback()
        log(db.cursor(), ACT_CONFIRM, FAILED, actor,
            staging_id=row["staging_id"], source_ref=row["source_ref"],
            sha=row["payload_sha256"],
            detail={"error": exc.__class__.__name__, "message": str(exc)[:500]})
        db.commit()
        raise
    db.commit()
    return {"product_id": product_id, "created": created,
            "already_confirmed": False, "rule_version": published,
            "merchant_enabled": 0}


# ---------------------------------------------------------------------------
# preview / withdraw / release
# ---------------------------------------------------------------------------

def _lens(cursor, product_id):
    row, _ = lens_view.load(cursor, product_id, SITE_COM)
    return row


def preview_link(cursor, staging_id, key, actor=None, base="https://optiwar.com",
                 hours=PREVIEW_HOURS):
    """A signed owner-only link to the confirmed, hidden product page."""
    row = staging_row(cursor, staging_id)
    if row is None or not row.get("product_id"):
        raise ImportRefused("not_confirmed",
                            "preview needs a confirmed staging row")
    lens = _lens(cursor, row["product_id"])
    if lens is None:
        raise ImportRefused("not_found", "product %s is not a contact lens"
                            % row["product_id"])
    if not lens_preview.previewable(lens):
        raise ImportRefused("not_previewable",
                            "the lens is not ready to be previewed",
                            {"release_blockers": list(lens["release_blockers"])})
    if not key:
        raise ImportRefused("no_preview_secret",
                            "the server has no preview signing secret")
    token = lens_preview.issue(key, lens["product_id"], hours)
    url = "%s/categories/contact-lenses/%s?pid=%s&preview=%s" % (
        base.rstrip("/"), lens["product_slug"], lens["product_id"], token)
    log(cursor, ACT_PREVIEW, OK, actor, staging_id=row["staging_id"],
        product_id=lens["product_id"], source_ref=row["source_ref"],
        detail={"hours": hours})
    return {"product_id": lens["product_id"], "url": url, "hours": hours}


def withdraw(cursor, product_id, actor):
    """Take a lens off sale everywhere without deleting anything of it."""
    if not _actor(actor):
        raise ImportRefused("actor_required", "withdraw names the human")
    lens = _lens(cursor, product_id)
    if lens is None:
        raise ImportRefused("not_found", "product %s is not a contact lens"
                            % product_id)
    cursor.execute("UPDATE contact_lens_products SET merchant_enabled=0"
                   " WHERE product_id=%s", (lens["product_id"],))
    for table in ("contact_lens_variants", "contact_lens_param_rules"):
        lens_import_write.withdraw_all(cursor, table, lens["product_id"])
    cursor.execute("UPDATE contact_lens_images SET image_type='WITHDRAWN',"
                   " gmc_eligible=0 WHERE product_id=%s AND"
                   " image_type<>'WITHDRAWN'", (lens["product_id"],))
    log(cursor, ACT_WITHDRAW, OK, actor, product_id=lens["product_id"],
        source_ref=lens.get("source_ref"),
        detail={"was_released": int(lens.get("merchant_enabled") or 0)})
    return {"product_id": lens["product_id"], "merchant_enabled": 0}


def release_blockers(lens):
    """Why the lens may not be released: the shared gate, plus the feed's.

    ``merchant_enabled=0`` is what release changes, so it is not a blocker
    here; a GTIN without the power it was read from is, because the feed
    would otherwise have to send a GTIN it cannot vouch for.
    """
    blockers = [b for b in catalogue.lens_release_blockers(lens, SITE_COM)
                if b != lens_preview.NOT_RELEASED]
    if (lens.get("gtin") or "").strip() and not (
            lens.get("gtin_reference_power") or "").strip():
        blockers.append("gtin without gtin_reference_power")
    return tuple(blockers)


def release(cursor, product_id, actor):
    """``merchant_enabled=1``, only when nothing else stands in the way."""
    if not _actor(actor):
        raise ImportRefused("actor_required", "release names the human")
    lens = _lens(cursor, product_id)
    if lens is None:
        raise ImportRefused("not_found", "product %s is not a contact lens"
                            % product_id)
    blockers = release_blockers(lens)
    if blockers:
        log(cursor, ACT_RELEASE, REFUSED, actor, product_id=lens["product_id"],
            source_ref=lens.get("source_ref"),
            detail={"release_blockers": list(blockers)})
        raise ImportRefused("not_ready", "the lens is not ready for release",
                            {"release_blockers": list(blockers)})
    cursor.execute("UPDATE contact_lens_products SET merchant_enabled=1"
                   " WHERE product_id=%s", (lens["product_id"],))
    log(cursor, ACT_RELEASE, OK, actor, product_id=lens["product_id"],
        source_ref=lens.get("source_ref"), detail={"site": SITE_COM})
    return {"product_id": lens["product_id"], "merchant_enabled": 1}


def audit_log(cursor, product_id=None, staging_id=None, limit=200):
    where, args = [], []
    if product_id is not None:
        where.append("product_id=%s")
        args.append(int(product_id))
    if staging_id is not None:
        where.append("staging_id=%s")
        args.append(int(staging_id))
    sql = "SELECT * FROM contact_lens_import_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY log_id DESC LIMIT %s"
    args.append(max(1, min(int(limit or 200), 1000)))
    cursor.execute(sql, tuple(args))
    out = []
    for row in cursor.fetchall() or ():
        row = dict(row)
        row["detail"] = _json_field(row, "detail_json")
        row.pop("detail_json", None)
        out.append(row)
    return out
