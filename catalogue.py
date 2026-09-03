"""Catalogue eligibility: which products exist for which storefront.

Both storefronts are one application over one ``products`` table; the host
decides currency and language, and until now every product was for sale on both.
Contact lenses are for ``optiwar.com`` only, and "only" has to mean the product
cannot be listed, searched, fed, API-returned, recommended or bought on
``optiwar.in`` — not merely that no menu links to it.

So eligibility is decided in exactly two functions, used by every read surface:

    catalogue_site_filter()   the SQL predicate a listing query appends
    is_product_allowed(row)   the decision a single fetched row is checked with

A surface that forgets to call one of these is a leak, which is why there is no
third way to make the decision and no per-surface ``if product_vertical ==``.

Both fail closed on the invariant and open on the status quo: a row whose
``sell_on_*`` column was not selected is still refused if it is a contact lens
on ``optiwar.in``, while a frame with no vertical recorded stays sellable
everywhere, exactly as it was before the column existed.
"""
import re

from flask import request

EYEWEAR = "EYEWEAR"
CONTACT_LENS = "CONTACT_LENS"

SITE_COM = "optiwar.com"
SITE_IN = "in.optiwar.com"

# Verticals a site sells. Frames are sold on both; the entry is explicit rather
# than a default so adding a vertical is a decision per site.
SITE_VERTICALS = {
    SITE_COM: (EYEWEAR, CONTACT_LENS),
    SITE_IN: (EYEWEAR,),
}

SITE_COLUMN = {SITE_COM: "sell_on_com", SITE_IN: "sell_on_in"}


def site_from_host(host):
    """``optiwar.in``, ``in.optiwar.com`` -> SITE_IN; anything else SITE_COM.

    Same test as ``get_site_from()`` in the app factory and ``_req_is_india()``,
    kept here so this module is usable without a request context (feeds, the
    importer, tests).
    """
    h = (host or "").lower()
    if "in.optiwar.com" in h or "optiwar.in" in h:
        return SITE_IN
    return SITE_COM


def current_site():
    """The storefront this request belongs to, SITE_COM outside a request."""
    try:
        return site_from_host(request.host)
    except RuntimeError:
        return SITE_COM


def site_column(site=None):
    return SITE_COLUMN[site or current_site()]


def catalogue_site_filter(site=None, alias=None):
    """The predicate every product listing/search/feed query must append.

    Returns e.g. ``" AND p.sell_on_in = 1"`` — a fragment with no parameters, so
    it composes with the f-string WHERE clauses the read paths already build.
    Existing frames default to 1 on both columns, so appending it changes no
    frame result.
    """
    col = site_column(site)
    return " AND %s%s = 1" % (("%s." % alias) if alias else "", col)


def vertical(product):
    v = (product or {}).get("product_vertical")
    v = (v or "").strip().upper()
    return v or EYEWEAR


def is_product_allowed(product, site=None):
    """Whether this product may be shown or sold on this storefront at all.

    Checked on every single-row path (product page, product API, add to cart)
    where a WHERE clause cannot do it. The column is authoritative when the
    query selected it; when it did not, the vertical decides, refusing anything
    the site does not sell rather than assuming a projection was complete.
    """
    if not product:
        return False
    site = site or current_site()
    flag = product.get(SITE_COLUMN[site])
    if flag not in (None, ""):
        try:
            return int(flag) == 1
        except (TypeError, ValueError):
            return False
    return vertical(product) in SITE_VERTICALS[site]


def is_contact_lens(product):
    return vertical(product) == CONTACT_LENS


# Paths that only exist for a site selling contact lenses. Used to keep the
# vertical out of the pre-generated sitemap, which is one file for both hosts.
_LENS_PATHS = ("/contact_lenses", "/contact-lenses",
               "/categories/contact-lenses/")

_URL_BLOCK = re.compile(r"<url>.*?</url>", re.DOTALL)


def strip_ineligible_urls(xml, site=None):
    """Drop <url> entries a site must not advertise from generated sitemap XML.

    A filter on output, not a substitute for the query filter: the file is built
    once and served to both hosts, so this is the only point at which .in can be
    prevented from publishing a lens URL it must not have.
    """
    if (site or current_site()) != SITE_IN:
        return xml

    def keep(match):
        block = match.group(0)
        return "" if any(p in block for p in _LENS_PATHS) else block

    return _URL_BLOCK.sub(keep, xml)


def sellable_here(cursor, product_id, site=None):
    """Whether this storefront may put this product in a cart.

    ``SELECT *`` rather than the three columns by name so a database that has
    not had the migration applied yet answers "allowed" for frames instead of
    raising Unknown column, and an unknown product_id is left to the caller's
    own handling — this guard exists to refuse the wrong storefront, not to
    become a second product-exists check.
    """
    cursor.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
    row = cursor.fetchone()
    if not row:
        return True
    return is_product_allowed(row, site)


# ---------------------------------------------------------------------------
# Release: which contact lenses a customer-facing surface may mention.
#
# Site eligibility says a lens *belongs* to this storefront. Release says it is
# finished: a lens row can exist for weeks with no images, no price and no
# matrix while the catalogue is loaded, and none of the surfaces — product page,
# search, sitemap, JSON-LD, the merchant feed, the model's prompt — may show a
# half-loaded one. One question, asked in one place, so they cannot disagree
# about what "live" means.
# ---------------------------------------------------------------------------

LENS_LIVE_SQL = """
SELECT p.product_id, p.product_code, p.product_name, p.product_slug,
       p.product_image, p.product_status, p.product_vertical,
       p.sell_on_com, p.sell_on_in,
       p.product_price_eur, p.product_special_price_eur,
       p.product_price, p.product_special_price,
       c.brand, c.manufacturer, c.gtin, c.manufacturer_mpn,
       c.modality, c.lens_type, c.pack_quantity, c.material,
       c.water_content, c.silicone_hydrogel, c.replacement_days,
       c.availability, c.lead_time_days, c.expected_available_at,
       c.prescription_required, c.color_enabled, c.merchant_enabled,
       c.param_mode, c.param_source,
       c.min_boxes_single_eye, c.min_boxes_both_per_eye,
       c.eur_inr_rate, c.eur_inr_rate_at,
       (SELECT COUNT(*) FROM contact_lens_variants v
         WHERE v.product_id = p.product_id AND v.available = 1) AS variant_count,
       (SELECT COUNT(*) FROM contact_lens_param_rules r
         WHERE r.product_id = p.product_id AND r.available = 1) AS rule_count,
       (SELECT COUNT(*) FROM contact_lens_images i
         WHERE i.product_id = p.product_id
           AND i.image_type <> 'WITHDRAWN') AS image_count
FROM contact_lens_products c
JOIN products p ON p.product_id = c.product_id
WHERE p.product_vertical = %s
"""

_DEAD_STATUSES = ("DISCONTINUED", "ARCHIVED")


def _positive(value):
    try:
        return float(value or 0) > 0
    except (TypeError, ValueError):
        return False


def lens_release_blockers(row, site=None):
    """Why this lens is not live on this storefront, empty when it is.

    Returned as reasons rather than a boolean because every caller that has to
    explain itself — the daily report, the importer, the merchant readiness
    check — otherwise re-derives them and gets a different list.
    """
    site = site or current_site()
    missing = []
    if not is_product_allowed(row, site):
        missing.append("not sold on %s" % site)
    try:
        released = int(row.get("merchant_enabled") or 0) == 1
    except (TypeError, ValueError):
        released = False
    if not released:
        missing.append("merchant_enabled=0")
    if (row.get("product_status") or "").strip().upper() in _DEAD_STATUSES:
        missing.append("status %s" % row.get("product_status"))
    if not (row.get("product_slug") or "").strip():
        missing.append("no landing page")
    if not (row.get("product_image") or "").strip() and not row.get("image_count"):
        missing.append("no primary image")
    if not (_positive(row.get("product_special_price_eur"))
            or _positive(row.get("product_price_eur"))):
        missing.append("no EUR price")
    if not (row.get("availability") or "").strip():
        missing.append("no availability")
    if not (row.get("brand") or "").strip():
        missing.append("no brand")
    # A missing identifier does not block release: a supplier that holds no
    # GTIN and no manufacturer part number for a lens is the ordinary case, and
    # the honest submission is identifier_exists=false (lens_feed). Inventing
    # one, or sending our own product_code as the manufacturer's, is the thing
    # that must not happen — so nothing here supplies a substitute.
    if not _stated(row):
        missing.append("no selectable values stated"
                       if _rule_mode(row) else "no prescription matrix")
    return tuple(missing)


def _stated(row):
    """Whether this lens says what may be ordered, in either shape.

    A RULES lens has no variant rows and is not half-loaded for it: the sphere
    list is what makes it orderable. Counting only combinations would hold every
    such lens back forever.
    """
    return bool(row.get("rule_count") if _rule_mode(row)
                else row.get("variant_count"))


def _rule_mode(row):
    return (row.get("param_mode") or "MATRIX").strip().upper() == "RULES"


def is_lens_live(row, site=None):
    return not lens_release_blockers(row, site)


def lens_rows(cursor, site=None):
    """Every contact lens with its profile, released or not, with blockers."""
    site = site or current_site()
    cursor.execute(LENS_LIVE_SQL + catalogue_site_filter(site, alias="p"),
                   (CONTACT_LENS,))
    rows = list(cursor.fetchall() or ())
    for row in rows:
        row["release_blockers"] = lens_release_blockers(row, site)
    return rows


def live_lenses(cursor, site=None):
    """The lenses this storefront may show, recommend, index and feed.

    On optiwar.in this is empty by construction — the site predicate is in the
    query and the release check repeats it on the row — so a surface built on
    this function cannot leak the vertical by forgetting a filter.
    """
    return [r for r in lens_rows(cursor, site) if not r["release_blockers"]]


LENS_MATRIX_SQL = """
SELECT COUNT(*) AS variants,
       MIN(sph) AS sph_min, MAX(sph) AS sph_max,
       MIN(cyl) AS cyl_min, MAX(cyl) AS cyl_max,
       MIN(axis) AS axis_min, MAX(axis) AS axis_max,
       MIN(add_power) AS add_min, MAX(add_power) AS add_max,
       MIN(base_curve) AS bc_min, MAX(base_curve) AS bc_max,
       MIN(diameter) AS dia_min, MAX(diameter) AS dia_max
FROM contact_lens_variants
WHERE product_id = %s AND available = 1
"""

LENS_COLORS_SQL = """
SELECT DISTINCT color_code, color_name
FROM contact_lens_variants
WHERE product_id = %s AND available = 1 AND color_code <> ''
ORDER BY color_name, color_code
"""


def lens_matrix_summary(cursor, product_id):
    """The range the matrix actually holds for one lens.

    A summary for describing a lens ("−0.50 to −6.00, cyl −0.75 to −1.75"), and
    explicitly not a way to decide an order: presence of a row is the only thing
    that makes a combination orderable, because a range says nothing about the
    step or about the holes real manufacturers leave in it.
    """
    cursor.execute(LENS_MATRIX_SQL, (product_id,))
    summary = dict(cursor.fetchone() or {})
    cursor.execute(LENS_COLORS_SQL, (product_id,))
    summary["colors"] = [(r.get("color_code"), r.get("color_name"))
                         for r in (cursor.fetchall() or ())]
    return summary


# Google's five permitted values, narrowest first.
AGE_GROUPS = ("newborn", "infant", "toddler", "kids", "adult")

# The canonical assignment, additive and nullable: a value is put there by a
# person, and NULL means nobody has decided yet.
GMC_COLUMNS = (("gmc_age_group", "VARCHAR(20) NULL"),)

_GMC_SCHEMA_READY = False


def ensure_gmc_columns(cursor):
    """Add ``GMC_COLUMNS`` to ``products`` if absent. Once per process.

    ``deploy/deploy.py migrate`` reads the declaration above and applies it
    deliberately, ahead of the code that selects it; this exists so a fresh
    database — a test, a new node — is not a special case, and so the feed
    cannot be deployed onto a schema that would make its query fail.
    """
    global _GMC_SCHEMA_READY
    if _GMC_SCHEMA_READY:
        return
    cursor.execute("SHOW COLUMNS FROM products")
    have = {(row.get("Field") if isinstance(row, dict) else row[0])
            for row in cursor.fetchall()}
    for name, decl in GMC_COLUMNS:
        if name not in have:
            cursor.execute("ALTER TABLE products ADD COLUMN %s %s"
                           % (name, decl))
    _GMC_SCHEMA_READY = True


def age_group(product):
    """Google ``age_group`` for a product, or "" to omit the attribute.

    Two signals, in order: an assignment somebody made (``gmc_age_group``), then
    ``product_category_kids`` — the catalogue's own flag for a children's frame.

    Words are deliberately not consulted. "BABY CAT 1402" reads as an infant
    frame and "LOUIS STYLE K-8003A" reads as an adult one, yet both are
    ``product_category_kids = 1``; the difference between newborn, infant,
    toddler and kids is an intended age range, which a product name does not
    state and a frame size does not imply.

    So a kids frame with no assignment returns "" and stays demoted, which is
    the smaller harm: a demotion is visible and reversible, while an offer
    labelled ``adult`` on a children's frame is wrong to every shopper who reads
    it. "" is also the answer when ``product_category_kids`` was not selected —
    a signal that was not fetched is not evidence that a product is for adults.
    """
    if not product:
        return ""
    assigned = str(product.get("gmc_age_group") or "").strip().lower()
    if assigned in AGE_GROUPS:
        return assigned
    kids = product.get("product_category_kids")
    if kids is None:
        return ""
    try:
        kids = int(kids)
    except (TypeError, ValueError):
        return ""
    return "" if kids else "adult"
