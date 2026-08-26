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
_LENS_PATHS = ("/contact_lenses", "/categories/contact-lenses/")

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
