"""Google Merchant offers for contact lenses, from the manufacturer's identity.

The frame feed sends ``brand=Optiwar`` and ``mpn=product_code``, which is true
of a frame we assemble and false of a box made by Alcon: a lens carries the
manufacturer's brand, its GTIN and its MPN, and our ``product_code`` is only our
own offer id. Sending ours as the manufacturer's would be a misrepresentation
Google disapproves, so lenses were excluded from that feed rather than
mislabelled by it, and this module is what replaces the exclusion.

Nothing here reads a database or a request: an offer is built from one row of
``catalogue.live_lenses()``, so what the feed publishes is decided by the same
release gate as the product page, the prompt and the sitemap, and a test can
assert the XML without a box.

EUR only, and .com only. A lens is not eligible on optiwar.in, and
``live_lenses()`` returns nothing there; ``lens_items()`` refuses an India feed
anyway, because a feed is the one surface that would send an offer to Google
regardless of who could see it on the storefront.
"""

import datetime
import os
from xml.sax.saxutils import escape as _xesc

# Google's five permitted availability values, of which a lens uses two:
# 'in_stock' ships now, 'backorder' is purchasable with a date. 'out_of_stock'
# is deliberately unreachable — a replenished lens is not depleted by an order,
# and a frame's product_quantity says nothing about one.
AVAILABILITY = {"IN_STOCK": "in_stock", "ON_ORDER": "backorder"}

MODALITY_WORDS = {
    "DAILY": "Daily disposable",
    "MONTHLY": "Monthly replacement",
    "CONVENTIONAL": "Conventional",
}

TYPE_WORDS = {
    "SPHERICAL": "Spherical",
    "TORIC": "Toric",
    "MULTIFOCAL": "Multifocal",
    "TORIC_MULTIFOCAL": "Toric multifocal",
    "COLOR": "Coloured",
}

LENS_IMAGES_SQL = """
SELECT image_url, image_type, sort_order, view_code, view_name, alt_text,
       gmc_eligible
FROM contact_lens_images
WHERE product_id = %s AND (color_code IS NULL OR color_code = '')
  AND image_type <> 'WITHDRAWN'
ORDER BY (image_type = 'PRIMARY') DESC, sort_order, image_id
"""


def lens_images(cursor, product_id):
    """Colour-independent imagery for one lens, primary first.

    Records rather than URLs, because every surface needs more than the path:
    the gallery needs the photographer's alt text and the feed needs to know
    which views are offer imagery. ``gmc`` defaults to True so a row loaded
    before those columns existed is treated as it was.
    """
    cursor.execute(LENS_IMAGES_SQL, (product_id,))
    out = []
    for i, row in enumerate(cursor.fetchall() or ()):
        url = (row.get("image_url") or "").strip()
        if not url:
            continue
        out.append({
            "url": url,
            "code": _text(row.get("view_code")),
            "view": _text(row.get("view_name")),
            "alt": _text(row.get("alt_text")),
            "primary": _text(row.get("image_type")).upper() == "PRIMARY",
            "position": i + 1,
            "gmc": int(row.get("gmc_eligible") or 0) == 1
            if row.get("gmc_eligible") is not None else True,
        })
    return out


def image_url(entry):
    """The URL of an image record, or of a bare URL."""
    if isinstance(entry, dict):
        return _text(entry.get("url") or entry.get("image_url"))
    return _text(entry)


def image_urls(row, base, gmc_only=False):
    """Absolute image URLs for one lens, primary first, deduped.

    ``gmc_only`` drops views a merchant offer must not carry — the label sample
    states one physical box's power, and an offer covers the whole matrix.
    """
    out = []
    for entry in [row.get("product_image")] + list(row.get("images") or ()):
        if gmc_only and isinstance(entry, dict) and not entry.get("gmc", True):
            continue
        url = media_url(image_url(entry), base)
        if url and url not in out:
            out.append(url)
    return out


# Image paths are stored the way ``products.product_image`` is — relative to
# ``static/``, e.g. ``catalog/contact-lenses/PRECISION1/01_hero.jpg`` — so one
# path serves the template, the derivative manifest and the public URL.
STATIC_PREFIX = "static/"


def media_url(path, base):
    """The public URL of a stored image path.

    A path already absolute, or already rooted at ``static/``, is left alone:
    a legacy row that stored a full URL keeps working.
    """
    p = _text(path).lstrip("/")
    if p.startswith("./"):
        p = p[2:]
    if p and not p.startswith("http") and not p.startswith(STATIC_PREFIX):
        p = STATIC_PREFIX + p
    return absolute(p, base)


def absolute(url, base):
    u = (url or "").strip()
    if not u or u.startswith("http://") or u.startswith("https://"):
        return u
    return "%s/%s" % (base.rstrip("/"), u.lstrip("/"))


def lens_link(row, base):
    return "%s/categories/contact-lenses/%s?pid=%s" % (
        base.rstrip("/"), row.get("product_slug") or "", row.get("product_id"))


def _text(value):
    return ("" if value is None else str(value)).strip()


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _number(value):
    """A decimal without trailing zeros: 8.60 -> 8.6, 30.00 -> 30."""
    text = _text(value)
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".")


def lens_title(row):
    """Brand, product and pack — what a shopper searches, once each.

    The manufacturer's own product name is the authority; brand and pack are
    prefixed and suffixed only when the name does not already state them, so
    "CooperVision MyDay Toric 30 Pack" does not become "CooperVision
    CooperVision MyDay Toric 30 Pack 30 Pack".
    """
    name = _text(row.get("product_name"))
    brand = _text(row.get("brand"))
    parts = []
    if brand and brand.lower() not in name.lower():
        parts.append(brand)
    parts.append(name)
    pack = _int(row.get("pack_quantity"))
    title = " ".join(p for p in parts if p)
    if pack and "pack" not in title.lower():
        title = "%s %d Pack" % (title, pack)
    return title.strip()


def lens_product_type(row):
    """``Contact Lenses > Daily > Toric`` — the breadcrumb, from the record."""
    crumbs = ["Contact Lenses"]
    modality = MODALITY_WORDS.get(_text(row.get("modality")).upper())
    if modality:
        crumbs.append(modality.split()[0])
    kind = TYPE_WORDS.get(_text(row.get("lens_type")).upper())
    if kind:
        crumbs.append(kind)
    return " > ".join(crumbs)


def lens_description(row):
    """Sentences assembled from stated facts, and from nothing else.

    Wear time, comfort claims and suitability are absent because the record does
    not state them, and a description is where an invented medical claim would
    otherwise appear.
    """
    kind = TYPE_WORDS.get(_text(row.get("lens_type")).upper(), "")
    modality = MODALITY_WORDS.get(_text(row.get("modality")).upper(), "")
    pack = _int(row.get("pack_quantity"))
    sentences = [lens_title(row)]
    lead = " ".join(x for x in (kind.lower(), "contact lenses") if x)
    if modality:
        sentences.append("%s %s." % (modality, lead))
    else:
        sentences.append("%s." % lead.capitalize())
    if pack:
        sentences.append("Box of %d lenses." % pack)
    spec = []
    if _text(row.get("material")):
        spec.append(_text(row.get("material")))
    water = _number(row.get("water_content"))
    if water:
        spec.append("%s%% water content" % water)
    if spec:
        sentences.append("%s." % ", ".join(spec))
    if _int(row.get("prescription_required")):
        sentences.append("Supplied to your prescription; "
                         "select power per eye at checkout.")
    return " ".join(s for s in sentences if s).strip()


def lens_availability(row, today=None):
    """``(availability, availability_date)``.

    An ON_ORDER lens is a backorder, and Google requires the date a backorder
    becomes available: the stated date when there is one, otherwise today plus
    the lead time the importer insisted on. A missing lead time cannot arrive
    here — ``lens_release_blockers()`` holds such a lens back — so the fallback
    is in_stock rather than a guessed date.
    """
    state = _text(row.get("availability")).upper()
    if state != "ON_ORDER":
        return AVAILABILITY.get(state, "in_stock"), ""
    stated = row.get("expected_available_at")
    if isinstance(stated, (datetime.datetime, datetime.date)):
        return "backorder", stated.strftime("%Y-%m-%d")
    lead = _int(row.get("lead_time_days"))
    if lead > 0:
        day = (today or datetime.date.today()) + datetime.timedelta(days=lead)
        return "backorder", day.strftime("%Y-%m-%d")
    return "in_stock", ""


def lens_price(row):
    """``(price, sale_price)`` in EUR, sale only when it differs from list."""
    regular = _text(row.get("product_price_eur"))
    special = _text(row.get("product_special_price_eur"))
    try:
        has_special = float(special or 0) > 0
        differs = has_special and float(special) != float(regular or 0)
    except (TypeError, ValueError):
        has_special, differs = False, False
    if not _text(regular) or float(regular or 0) <= 0:
        return special, ""
    return regular, (special if differs else "")


def lens_details(row):
    """``product_detail`` triples: the manufacturer's specification, verbatim.

    Base curve and diameter live on the variants because a lens can be sold in
    more than one, so they appear here only when the caller resolved them for
    the offer — an offer-level BC on a two-BC lens would be false for half its
    matrix.
    """
    out = []
    pack = _int(row.get("pack_quantity"))
    if pack:
        out.append(("Lens", "Pack size", "%d lenses" % pack))
    modality = MODALITY_WORDS.get(_text(row.get("modality")).upper())
    if modality:
        out.append(("Lens", "Replacement schedule", modality))
    days = _int(row.get("replacement_days"))
    if days:
        out.append(("Lens", "Replacement days", str(days)))
    kind = TYPE_WORDS.get(_text(row.get("lens_type")).upper())
    if kind:
        out.append(("Lens", "Lens type", kind))
    if _text(row.get("material")):
        out.append(("Lens", "Material", _text(row.get("material"))))
    if _int(row.get("silicone_hydrogel")):
        out.append(("Lens", "Silicone hydrogel", "Yes"))
    water = _number(row.get("water_content"))
    if water:
        out.append(("Lens", "Water content", "%s%%" % water))
    for name, key in (("Base curve", "base_curve"), ("Diameter", "diameter")):
        value = _number(row.get(key))
        if value:
            out.append(("Lens", name, "%s mm" % value))
    return out


def lens_offer(row, base, today=None):
    """One offer as ordered ``(tag, value)`` pairs, or ``None`` if not live.

    Identifiers are the manufacturer's or nobody's: a real GTIN and a real MPN
    are sent when they exist, and when the supplier holds neither the offer says
    ``identifier_exists=false`` rather than sending our own ``product_code`` as
    the manufacturer's part number, which is what makes an offer collide with
    somebody else's product.
    """
    if row.get("release_blockers"):
        return None
    price, sale = lens_price(row)
    if not price:
        return None
    availability, available_on = lens_availability(row, today)
    images = image_urls(row, base, gmc_only=True)
    if not images:
        return None
    fields = [
        ("g:id", _text(row.get("product_code")) or _text(row.get("product_id"))),
        ("g:title", lens_title(row)),
        ("g:description", lens_description(row)),
        ("g:link", lens_link(row, base)),
        ("g:image_link", images[0]),
    ]
    fields += [("g:additional_image_link", u) for u in images[1:11]]
    fields.append(("g:availability", availability))
    if available_on:
        fields.append(("g:availability_date", available_on))
    fields.append(("g:price", "%s EUR" % price))
    if sale:
        fields.append(("g:sale_price", "%s EUR" % sale))
    fields.append(("g:condition", "new"))
    fields.append(("g:brand", _text(row.get("brand"))))
    gtin, mpn = _text(row.get("gtin")), _text(row.get("manufacturer_mpn"))
    if gtin:
        fields.append(("g:gtin", gtin))
    if mpn:
        fields.append(("g:mpn", mpn))
    if not (gtin or mpn):
        fields.append(("g:identifier_exists", "false"))
    # Taxonomy id only when somebody has verified it in Merchant Center, for the
    # reason the frame feed dropped its hardcoded 178: a wrong category is worse
    # than none, because Google categorises a well-described offer itself.
    category = _text(os.environ.get("GMC_LENS_CATEGORY"))
    if category:
        fields.append(("g:google_product_category", category))
    fields.append(("g:product_type", lens_product_type(row)))
    fields.append(("g:custom_label_0", _text(row.get("modality")).upper()))
    fields.append(("g:custom_label_1", _text(row.get("lens_type")).upper()))
    return [(tag, value) for tag, value in fields if _text(value)]


def lens_item_xml(row, base, today=None):
    """One ``<item>`` block, or "" for a lens that is not a publishable offer."""
    offer = lens_offer(row, base, today)
    if not offer:
        return ""
    lines = ["    <item>"]
    lines += ["      <%s>%s</%s>" % (tag, _xesc(str(value)), tag)
              for tag, value in offer]
    for section, name, value in lens_details(row):
        lines += ["      <g:product_detail>",
                  "        <g:section_name>%s</g:section_name>" % _xesc(section),
                  "        <g:attribute_name>%s</g:attribute_name>" % _xesc(name),
                  "        <g:attribute_value>%s</g:attribute_value>"
                  % _xesc(str(value)),
                  "      </g:product_detail>"]
    lines.append("    </item>")
    return "\n".join(lines)


def lens_items(rows, base, is_india=False, today=None):
    """Publishable lens offers. Empty for the India feed, by construction."""
    if is_india:
        return []
    return [xml for xml in (lens_item_xml(row, base, today) for row in rows)
            if xml]
