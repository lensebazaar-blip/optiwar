"""What a search engine and an answer engine are told about a contact lens.

One release flag decides every surface. A lens that ``catalogue.live_lenses()``
returns has a product page, a canonical URL, Product and Breadcrumb JSON-LD, a
sitemap entry, an image sitemap entry, a place in the category tree, a brand
landing page and a Merchant offer; a lens it does not return has none of them.
That is the whole point of the flag: ~70 rows can be loaded while four are
released, and no surface gets to decide for itself what "live" means.

Two things are deliberately absent, both because the record does not state them:

* No frame claims. The frame product page promises complimentary prescription
  lenses, answers "what size is this frame", and carries a HowTo about choosing
  lens type. Every one of those is false of a box of lenses, so a lens page emits
  this module's JSON-LD *instead of* the frame template's, not alongside it.
* No shipping or returns terms. The frame markup states a 7-day return window
  and free worldwide delivery. Returning an opened medical device is not the same
  promise in every market, and counsel has not approved lens wording, so nothing
  is asserted rather than asserting the frame policy.

Facets are the category tree, and prescription combinations are not in it: a
crawlable URL per power/cylinder/axis would be thousands of near-identical thin
pages for one product. The matrix belongs on the page.

Nothing here reads a database or a request, so a page's markup is a function of
its rows.
"""

import json
from xml.sax.saxutils import escape as _xesc

try:                             # inside the flask package
    from . import lens_feed
except ImportError:              # loaded standalone by a test
    import lens_feed

ROOT_PATH = "/contact-lenses"
ROOT_LABEL = "Contact Lenses"
BRAND_PATH = ROOT_PATH + "/brand"

# Contact Lenses -> Daily / Monthly / Toric / Multifocal / Toric Multifocal /
# Coloured. A facet is a field of the canonical record, so a page exists exactly
# when lenses of that kind are released — never as an empty shelf.
FACETS = (
    ("daily", "Daily", "modality", "DAILY"),
    ("monthly", "Monthly", "modality", "MONTHLY"),
    ("conventional", "Conventional", "modality", "CONVENTIONAL"),
    ("toric", "Toric", "lens_type", "TORIC"),
    ("multifocal", "Multifocal", "lens_type", "MULTIFOCAL"),
    ("toric-multifocal", "Toric Multifocal", "lens_type", "TORIC_MULTIFOCAL"),
    ("coloured", "Coloured", "lens_type", "COLOR"),
)

SCHEMA_AVAILABILITY = {
    "in_stock": "https://schema.org/InStock",
    "backorder": "https://schema.org/BackOrder",
}


def _text(value):
    return ("" if value is None else str(value)).strip()


def slugify(value):
    out = []
    for ch in _text(value).lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


def facet(slug):
    """The facet definition for a URL segment, or None."""
    for entry in FACETS:
        if entry[0] == slug:
            return entry
    return None


def in_facet(row, slug):
    entry = facet(slug)
    if not entry:
        return False
    _, _, field, value = entry
    return _text(row.get(field)).upper() == value


def lens_path(row):
    return "/categories/contact-lenses/%s?pid=%s" % (
        _text(row.get("product_slug")), row.get("product_id"))


def lens_url(row, base):
    return "%s%s" % (base.rstrip("/"), lens_path(row))


def facet_pages(rows):
    """Facet landing pages that have at least one released lens.

    Order follows ``FACETS`` so the tree is stable, and a facet with nothing in
    it is not a page: a thin shelf is worse for search than no shelf.
    """
    pages = []
    for slug, label, _field, _value in FACETS:
        members = [r for r in rows if in_facet(r, slug)]
        if members:
            pages.append({"slug": slug, "label": label,
                          "path": "%s/%s" % (ROOT_PATH, slug),
                          "rows": members})
    return pages


def brand_pages(rows):
    """One landing page per manufacturer brand actually released."""
    order, groups = [], {}
    for row in rows:
        brand = _text(row.get("brand"))
        if not brand:
            continue
        slug = slugify(brand)
        if slug not in groups:
            order.append(slug)
            groups[slug] = {"slug": slug, "label": brand,
                            "path": "%s/%s" % (BRAND_PATH, slug), "rows": []}
        groups[slug]["rows"].append(row)
    return [groups[s] for s in order]


def images(row, base):
    """Absolute image URLs, primary first, deduped."""
    out = []
    for url in [row.get("product_image")] + list(row.get("images") or ()):
        absolute = lens_feed.absolute(url, base)
        if absolute and absolute not in out:
            out.append(absolute)
    return out


def _properties(row, matrix=None):
    props = [{"@type": "PropertyValue", "name": name, "value": value}
             for _section, name, value in lens_feed.lens_details(row)]
    if not matrix:
        return props
    # The matrix, stated as what it is: a count of combinations the
    # manufacturer actually supplies, plus the outer bounds. The bounds are not
    # a promise that every pair inside them exists, which is why the count is
    # named and why no page enumerates them.
    ranges = (("Sphere powers", "sph_min", "sph_max"),
              ("Cylinder powers", "cyl_min", "cyl_max"),
              ("Axis", "axis_min", "axis_max"),
              ("Add powers", "add_min", "add_max"))
    for label, low, high in ranges:
        lo, hi = matrix.get(low), matrix.get(high)
        if lo is None or hi is None:
            continue
        props.append({"@type": "PropertyValue", "name": label,
                      "value": "%s to %s" % (_number(lo), _number(hi))})
    if matrix.get("variants"):
        props.append({"@type": "PropertyValue",
                      "name": "Prescription combinations available",
                      "value": str(int(matrix["variants"]))})
    return props


def _number(value):
    text = _text(value)
    if "." not in text:
        return text
    return text.rstrip("0").rstrip(".") or "0"


def product_jsonld(row, base, matrix=None):
    """schema.org Product for one released lens, or None.

    The identity is the manufacturer's — ``brand``, ``gtin``, ``mpn`` — and our
    ``product_code`` is the ``sku``, which is what a seller's own code is. The
    frame page sends ``mpn = product_code`` and ``brand = Optiwar``; doing that
    here would tell Google we manufacture an Alcon lens.
    """
    if row.get("release_blockers"):
        return None
    price, sale = lens_feed.lens_price(row)
    if not price:
        return None
    availability, available_on = lens_feed.lens_availability(row)
    url = lens_url(row, base)
    data = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": lens_feed.lens_title(row),
        "description": lens_feed.lens_description(row),
        "url": url,
        "sku": _text(row.get("product_code")),
        "brand": {"@type": "Brand", "name": _text(row.get("brand"))},
        "category": lens_feed.lens_product_type(row),
        "itemCondition": "https://schema.org/NewCondition",
    }
    if _text(row.get("manufacturer")):
        data["manufacturer"] = {"@type": "Organization",
                                "name": _text(row.get("manufacturer"))}
    if _text(row.get("gtin")):
        data["gtin"] = _text(row.get("gtin"))
    if _text(row.get("manufacturer_mpn")):
        data["mpn"] = _text(row.get("manufacturer_mpn"))
    picture = images(row, base)
    if picture:
        data["image"] = picture
    props = _properties(row, matrix)
    if props:
        data["additionalProperty"] = props
    offer = {
        "@type": "Offer",
        "url": url,
        "price": sale or price,
        "priceCurrency": "EUR",
        "availability": SCHEMA_AVAILABILITY.get(
            availability, SCHEMA_AVAILABILITY["in_stock"]),
        "itemCondition": "https://schema.org/NewCondition",
        "seller": {"@type": "Organization", "name": "Optiwar"},
    }
    if available_on:
        offer["availabilityStarts"] = available_on
    data["offers"] = offer
    return data


def breadcrumb_jsonld(row, base):
    """Home > Contact Lenses > <facet> > product, matching the visible trail."""
    root = base.rstrip("/")
    crumbs = [("Home", root + "/"), (ROOT_LABEL, root + ROOT_PATH)]
    for slug, label, _field, _value in FACETS:
        if in_facet(row, slug):
            crumbs.append((label, "%s%s/%s" % (root, ROOT_PATH, slug)))
            break
    crumbs.append((lens_feed.lens_title(row), lens_url(row, base)))
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": item}
            for i, (name, item) in enumerate(crumbs)],
    }


def collection_jsonld(label, path, rows, base):
    """ItemList for a facet or brand landing page, in the page's own order."""
    root = base.rstrip("/")
    return {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": label,
        "url": root + path,
        "mainEntity": {
            "@type": "ItemList",
            "numberOfItems": len(rows),
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1,
                 "name": lens_feed.lens_title(r), "url": lens_url(r, base)}
                for i, r in enumerate(rows)],
        },
    }


def jsonld_blocks(row, base, matrix=None):
    """The JSON-LD a lens product page emits, serialised, ready for a template."""
    product = product_jsonld(row, base, matrix)
    if not product:
        return []
    return [json.dumps(product, indent=2, sort_keys=True),
            json.dumps(breadcrumb_jsonld(row, base), indent=2, sort_keys=True)]


AVAILABILITY_WORDS = {"in_stock": "In stock",
                      "backorder": "Available on order"}


def card(row, base):
    """One lens as a listing entry: what the record says, priced per box."""
    price, sale = lens_feed.lens_price(row)
    availability, available_on = lens_feed.lens_availability(row)
    word = AVAILABILITY_WORDS.get(availability, AVAILABILITY_WORDS["in_stock"])
    if available_on:
        word = "%s (expected %s)" % (word, available_on)
    spec = [lens_feed.MODALITY_WORDS.get(_text(row.get("modality")).upper(), ""),
            lens_feed.TYPE_WORDS.get(_text(row.get("lens_type")).upper(), "")]
    pack = row.get("pack_quantity")
    try:
        pack = int(pack)
    except (TypeError, ValueError):
        pack = 0
    if pack:
        spec.append("box of %d" % pack)
    picture = images(row, base)
    return {
        "path": lens_path(row),
        "title": lens_feed.lens_title(row),
        "image": picture[0] if picture else "",
        "spec": " \u00b7 ".join(s for s in spec if s),
        "price": sale or price,
        "list_price": price if sale else "",
        "availability": word,
    }


def landing_page(rows, base, facet_slug=None, brand_slug=None):
    """The view model for a lens landing page, or None when there is no page.

    None rather than an empty page for a facet or brand nothing is released in:
    an empty shelf is still a URL search has to judge, and it is thin by
    definition. The root page exists whenever any lens is released.
    """
    if not rows:
        return None
    if facet_slug and brand_slug:
        return None
    if facet_slug:
        page = next((p for p in facet_pages(rows) if p["slug"] == facet_slug),
                    None)
    elif brand_slug:
        page = next((p for p in brand_pages(rows) if p["slug"] == brand_slug),
                    None)
    else:
        page = {"slug": "", "label": ROOT_LABEL, "path": ROOT_PATH,
                "rows": list(rows)}
    if not page:
        return None
    members = page["rows"]
    heading = page["label"] if page["label"] == ROOT_LABEL else (
        "%s %s" % (page["label"], ROOT_LABEL))
    return {
        "label": page["label"],
        "heading": heading,
        "path": page["path"],
        "rows": [card(r, base) for r in members],
        "brands": [{"label": p["label"], "path": p["path"]}
                   for p in brand_pages(members)],
        "shelves": [{"label": p["label"], "path": p["path"]}
                    for p in facet_pages(rows)] if not page["slug"] else [],
        "jsonld": [json.dumps(collection_jsonld(page["label"], page["path"],
                                                members, base),
                              indent=2, sort_keys=True)],
    }


def sitemap_urls(rows, base, is_india=False):
    """``<url>`` blocks for every released lens and every non-empty shelf.

    Empty for optiwar.in whatever the rows say. The sitemap file is generated
    once and served to both hosts, so this is checked here as well as in the
    query that produced the rows.
    """
    if is_india:
        return []
    if not rows:
        return []
    root = base.rstrip("/")
    locs = [root + ROOT_PATH]
    locs += [root + page["path"] for page in facet_pages(rows)]
    locs += [root + page["path"] for page in brand_pages(rows)]
    locs += [lens_url(row, base) for row in rows]
    return ["  <url><loc>%s</loc></url>" % _xesc(loc) for loc in locs]


def image_sitemap_urls(rows, base, is_india=False):
    """``<url>`` blocks pairing each lens page with its imagery."""
    if is_india:
        return []
    blocks = []
    for row in rows:
        picture = images(row, base)
        if not picture:
            continue
        block = ["  <url>", "    <loc>%s</loc>" % _xesc(lens_url(row, base))]
        block += ["    <image:image><image:loc>%s</image:loc></image:image>"
                  % _xesc(url) for url in picture]
        block.append("  </url>")
        blocks.append("\n".join(block))
    return blocks
