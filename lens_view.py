"""The canonical commerce object for one contact lens, built once.

A lens page used to ask the database a question per fact — the manufacturer, the
price, the base curves, the images, the minimum, the availability, the SEO — and
every other consumer (the model, the feed, the sitemap, ops) asked its own set
and could get a different answer. This module asks once and hands back one
object: ``load()`` does the queries, ``passport()`` shapes them, and JSON-LD,
the gallery, the feed and Optiwar AI read fields off the result instead of
re-deriving them.

It states nothing the record does not. There is no wear-time, no comfort claim
and no invented identifier; the release state is derived from the gate rather
than stored, so no new lifecycle column exists to disagree with the gate:

    merchant_enabled=1, gate passes  ->  RELEASED
    merchant_enabled=0, nothing else ->  QA_READY
    anything else outstanding        ->  DRAFT

``passport()`` is pure. Given a row, a matrix summary and image records it
returns the same object in a test as in production, so what the PDP renders can
be asserted without a box.
"""

import datetime
import json

try:                             # inside the flask package
    from . import lens_feed, lens_seo
    from .catalogue import lens_matrix_summary, lens_rows, SITE_COM
except ImportError:              # loaded standalone by a test
    import lens_feed
    import lens_seo
    from catalogue import lens_matrix_summary, lens_rows, SITE_COM

SCHEMA_VERSION = 1

STATE_DRAFT = "DRAFT"
STATE_QA_READY = "QA_READY"
STATE_RELEASED = "RELEASED"

# The blocker the gate reports for a lens nobody has released yet. A lens whose
# only outstanding item is that flag is finished work waiting for a decision,
# which is what QA_READY means here.
NOT_RELEASED = "merchant_enabled=0"


def _text(value):
    return ("" if value is None else str(value)).strip()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _number(value):
    """A decimal without trailing zeros, or None: 8.30 -> '8.3'."""
    text = _text(value)
    if not text:
        return None
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".") or "0"


def _stamp(value):
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    return _text(value) or None


def release_state(row, blockers=None):
    """``RELEASED`` / ``QA_READY`` / ``DRAFT`` from the gate, not from a column."""
    outstanding = tuple(blockers if blockers is not None
                        else row.get("release_blockers") or ())
    if not outstanding:
        return STATE_RELEASED
    if tuple(outstanding) == (NOT_RELEASED,):
        return STATE_QA_READY
    return STATE_DRAFT


def prices(row):
    """EUR as given, INR as stored, and the rate that explains the pair.

    Neither currency is computed here. The importer converted once and recorded
    the rate it used; a page that re-converted would show a price nobody chose.
    """
    eur_list, eur_sale = lens_feed.lens_price(row)
    return {
        "currency": "EUR",
        "list": eur_list or None,
        "selling": eur_sale or eur_list or None,
        "inr": {
            "list": _number(row.get("product_price")),
            "selling": _number(row.get("product_special_price"))
                       or _number(row.get("product_price")),
            "rate": _number(row.get("eur_inr_rate")),
            "rate_at": _stamp(row.get("eur_inr_rate_at")),
        },
    }


def matrix_block(matrix):
    """The bounds and the count, and never an enumeration.

    The count is the number of combinations the source states. The bounds are
    the outer edges of those combinations and are not a promise that every pair
    inside them exists, which is why a caller cannot build a selection from
    this object — ``lens_order`` validates against the stored rows.
    """
    if not matrix:
        return None
    out = {"variants": _int(matrix.get("variants")) or 0}
    for name, low, high in (("sph", "sph_min", "sph_max"),
                            ("cyl", "cyl_min", "cyl_max"),
                            ("axis", "axis_min", "axis_max"),
                            ("add_power", "add_min", "add_max"),
                            ("base_curve", "bc_min", "bc_max"),
                            ("diameter", "dia_min", "dia_max")):
        lo, hi = _number(matrix.get(low)), _number(matrix.get(high))
        if lo is None or hi is None:
            continue
        out[name] = {"min": lo, "max": hi}
    colors = [{"code": _text(code), "name": _text(name)}
              for code, name in (matrix.get("colors") or ())
              if _text(code)]
    if colors:
        out["colors"] = colors
    return out


def image_block(row, base):
    """Approved views in gallery order, each with what a surface needs.

    ``gmc`` travels with the view rather than being decided per surface, and
    ``primary`` is the release-gated one: a lens with no primary image is held
    back by the gate, so a page never has to render a placeholder.
    """
    out = []
    for record in (row.get("images") or ()):
        url = lens_feed.image_url(record)
        if not url:
            continue
        entry = {
            "path": url,
            "url": lens_feed.media_url(url, base),
            "position": len(out) + 1,
        }
        if isinstance(record, dict):
            entry["code"] = record.get("code") or ""
            entry["view"] = record.get("view") or ""
            entry["alt"] = record.get("alt") or ""
            entry["primary"] = bool(record.get("primary"))
            entry["gmc"] = bool(record.get("gmc", True))
        else:
            entry["code"] = entry["view"] = entry["alt"] = ""
            entry["primary"] = len(out) == 0
            entry["gmc"] = True
        out.append(entry)
    if out and not any(e["primary"] for e in out):
        out[0]["primary"] = True
    return out


def passport(row, matrix=None, base="https://optiwar.com"):
    """One product, as one object. Pure: no database, no request, no clock."""
    blockers = tuple(row.get("release_blockers") or ())
    availability, available_on = lens_feed.lens_availability(row)
    images = image_block(row, base)
    data = {
        "schema_version": SCHEMA_VERSION,
        "product": {
            "id": _int(row.get("product_id")),
            "code": _text(row.get("product_code")),
            "name": _text(row.get("product_name")),
            "title": lens_feed.lens_title(row),
            "slug": _text(row.get("product_slug")),
            "vertical": _text(row.get("product_vertical")),
            "sites": {"com": bool(_int(row.get("sell_on_com"))),
                      "in": bool(_int(row.get("sell_on_in")))},
        },
        "identity": {
            "brand": _text(row.get("brand")),
            "manufacturer": _text(row.get("manufacturer")),
            "gtin": _text(row.get("gtin")) or None,
            "mpn": _text(row.get("manufacturer_mpn")) or None,
            "identifier_exists": bool(_text(row.get("gtin"))
                                      or _text(row.get("manufacturer_mpn"))),
        },
        "lens": {
            "modality": _text(row.get("modality")),
            "lens_type": _text(row.get("lens_type")),
            "pack_quantity": _int(row.get("pack_quantity")),
            "material": _text(row.get("material")) or None,
            "water_content": _number(row.get("water_content")),
            "silicone_hydrogel": bool(_int(row.get("silicone_hydrogel"))),
            "replacement_days": _int(row.get("replacement_days")),
            "prescription_required": bool(
                _int(row.get("prescription_required"))),
            "color_enabled": bool(_int(row.get("color_enabled"))),
        },
        "price": prices(row),
        "availability": {
            "state": _text(row.get("availability")).upper(),
            "google": availability,
            "available_on": available_on or None,
            "lead_time_days": _int(row.get("lead_time_days")),
        },
        "ordering": {
            "param_mode": _text(row.get("param_mode")).upper() or "MATRIX",
            "param_source": _text(row.get("param_source")) or None,
            "min_boxes_single_eye": _int(row.get("min_boxes_single_eye")),
            "min_boxes_both_per_eye": _int(row.get("min_boxes_both_per_eye")),
            "matrix": matrix_block(matrix),
        },
        "images": images,
        "release": {
            "merchant_enabled": bool(_int(row.get("merchant_enabled"))),
            "state": release_state(row, blockers),
            "blockers": list(blockers),
        },
        "seo": {
            "canonical": lens_seo.lens_url(row, base),
            "product_type": lens_feed.lens_product_type(row),
            "description": lens_feed.lens_description(row),
        },
        "warnings": list(row.get("image_warnings") or ()),
    }
    return data


def jsonld(row, matrix=None, base="https://optiwar.com"):
    """The page's JSON-LD, from the same row the passport was built from."""
    return lens_seo.jsonld_blocks(row, base, matrix)


def load(cursor, product_id, site=None, base="https://optiwar.com"):
    """``(row, passport)`` for one lens, or ``(None, None)``.

    Three queries — the lens with its gate, its matrix summary, its images —
    and every fact the page, the model and ops need comes off the result. The
    row is returned beside the object because JSON-LD and the feed are built
    from a row by design, and both must come from this one read.
    """
    site = site or SITE_COM
    wanted = _int(product_id)
    if wanted is None:
        return None, None
    row = next((r for r in lens_rows(cursor, site)
                if _int(r.get("product_id")) == wanted), None)
    if not row:
        return None, None
    row["images"] = lens_feed.lens_images(cursor, row["product_id"])
    matrix = lens_matrix_summary(cursor, row["product_id"])
    row["matrix"] = matrix
    return row, passport(row, matrix, base)


def load_released(cursor, product_id, site=None, base="https://optiwar.com"):
    """As ``load()``, but ``(None, None)`` unless the gate says released."""
    row, data = load(cursor, product_id, site, base)
    if not row or row.get("release_blockers"):
        return None, None
    return row, data


def as_json(data):
    return json.dumps(data, indent=2, sort_keys=True, default=str)
