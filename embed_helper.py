"""Backend helper for the responsive <picture> embed.

Register inside create_app() BEFORE returning the app:

    from .embed_helper import register_image_helpers
    register_image_helpers(app)

Loads static/catalog/derivative_manifest.tsv ONCE at startup into a dict of
{catalog-root-relative-path: sha256}. Exposes to Jinja:
  - img_has_derivatives(product_image_entry) -> bool
  - img_ver(path)  -> 12-char content-hash for ?v= cache busting ('' if unknown)
Lookups are EXACT (case-sensitive) against the manifest keys, so .jpg / .JPG /
.jpeg / .webp all resolve to their own real file; nothing assumes lowercase.jpg.
The version hash is the file's SHA-256 prefix, so a URL only changes when the
file's bytes change. No hashing is done per request.
"""
import os
import re

from flask import url_for, current_app

_VER = {}          # normalized relpath -> full sha256
_LOADED = False

_MULTISLASH = re.compile(r"/{2,}")

# Single source of truth for the responsive ladder. The Jinja macro
# (_picture.html) and build_media() below both derive URLs from these; no
# template or JS reconstructs derivative filenames independently.
DERIVATIVE_WIDTHS = (200, 400, 800, 1200, 2000)
DERIVATIVE_FORMATS = ("avif", "webp", "jpg")

# ── FROZEN media API contract (v1) ──────────────────────────────────────────
# A media object is EXACTLY these keys; consumers (ow_picture.js, templates)
# must not depend on any other key. Bump MEDIA_SCHEMA_VERSION on a breaking
# change and expose it via the API's top-level "media_schema" field so clients
# can detect/version. avif/webp/jpg are width-descriptor srcset strings (or ''
# when no derivative ladder exists). src/zoom are versioned master-JPEG URLs.
MEDIA_SCHEMA_VERSION = 1
MEDIA_KEYS = ("src", "zoom", "has_derivatives", "avif", "webp", "jpg")


def img_norm(path):
    """Normalize a product_image entry / derivative path to a manifest key.
    Strips leading './', collapses '//', strips a leading 'catalog/' prefix.
    Case is preserved (exact match)."""
    if not path:
        return ""
    p = path.strip()
    p = _MULTISLASH.sub("/", p)
    if p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    if p.startswith("catalog/"):
        p = p[len("catalog/"):]
    return p


def _load(app):
    global _LOADED
    mf = os.path.join(app.root_path, "static", "catalog", "derivative_manifest.tsv")
    try:
        with open(mf) as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                rel, _, h = line.partition("\t")
                if rel and h:
                    _VER[rel] = h
    except FileNotFoundError:
        app.logger.warning("derivative_manifest.tsv not found at %s", mf)
    _LOADED = True
    app.logger.info("embed manifest loaded: %d entries", len(_VER))


def img_has_derivatives(path):
    """True only when the master path has a full derivative ladder (its key is
    a master entry in the manifest, i.e. present and not under /derivatives/)."""
    key = img_norm(path)
    return key in _VER and "/derivatives/" not in key


def img_ver(path):
    """Return a stable 12-char content hash for ?v=, or '' if not in manifest."""
    return _VER.get(img_norm(path), "")[:12]


def _ver_url(static_filename):
    """url_for('static', ...) + ?v=<hash> (hash omitted if unknown)."""
    url = url_for("static", filename=static_filename)
    v = img_ver(static_filename)
    return url + ("?v=" + v if v else "")


def img_static_relpath(path):
    """product_image entry -> clean '/static/catalog/.../X.jpg' path (leading
    './' removed, no version). Empty string for empty input."""
    if not path:
        return ""
    p = _MULTISLASH.sub("/", path.strip())
    if p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return "/static/" + p


def versioned_image_url(path, base_url=""):
    """Absolute (if base_url given) or root-relative canonical MASTER image URL
    with ?v=<content-hash>. Used by the Merchant feed and JSON-LD so both point
    at the same stable, versioned master JPEG (never AVIF/WebP derivatives)."""
    rel = img_static_relpath(path)
    if not rel:
        return ""
    v = img_ver(path)
    return (base_url + rel) + ("?v=" + v if v else "")


# ---------------------------------------------------------------------------
# Product lifecycle resolvers (single source of truth; see the lifecycle design
# doc). These are PURE and side-effect free. Read surfaces (Merchant feed,
# sitemaps, browse listings, PDP) should all route through these so eligibility
# logic never diverges across files. Wiring the read paths onto them is rolled
# out as a controlled, behaviour-verified canary — not implicitly here.
# ---------------------------------------------------------------------------

LIFECYCLE_STATES = ("ACTIVE", "OUT_OF_STOCK", "SEASONAL", "DISCONTINUED", "ARCHIVED")


def _qty_int(product):
    try:
        return int(product.get("product_quantity") or 0)
    except (TypeError, ValueError):
        return 0


def lifecycle_status(product):
    """Normalized product_status, defaulting to a legacy-derived value when the
    column is absent/empty (keeps callers correct pre- and post-migration)."""
    s = (product.get("product_status") or "").strip().upper()
    if s in LIFECYCLE_STATES:
        return s
    if int(product.get("discontinued") or 0) == 1:
        return "DISCONTINUED"
    return "OUT_OF_STOCK" if _qty_int(product) <= 0 else "ACTIVE"


def should_retain_discontinued_page(product):
    """Whether a DISCONTINUED product should keep a live 200 page (default yes;
    fashion frames usually retain SEO/link value). Only ARCHIVED truly retires."""
    return lifecycle_status(product) == "DISCONTINUED"


def is_merchant_eligible(product):
    """Google Merchant feed inclusion: purchasable, active, in stock."""
    return lifecycle_status(product) == "ACTIVE" and _qty_int(product) > 0


def is_browse_eligible(product):
    """Whether to show in main category/listing pagination. OUT_OF_STOCK is
    listing-configurable via show_in_listings; PDP stays live regardless."""
    st = lifecycle_status(product)
    if st == "ACTIVE":
        return True
    if st == "OUT_OF_STOCK":
        flag = product.get("show_in_listings")
        return bool(int(flag)) if flag not in (None, "") else False
    return False  # SEASONAL / DISCONTINUED / ARCHIVED excluded from browse


def is_product_sitemap_eligible(product):
    st = lifecycle_status(product)
    if st in ("ACTIVE", "OUT_OF_STOCK", "SEASONAL"):
        return True
    if st == "DISCONTINUED":
        return should_retain_discontinued_page(product)
    return False  # ARCHIVED


def replacement_is_live(replacement):
    """A replacement may only receive traffic if it is itself purchasable."""
    return bool(replacement) and is_merchant_eligible(replacement)


def resolve_pdp(product, replacement=None):
    """Central PDP routing decision. Returns one of:
      ('render', 200) | ('render_unavailable', 200) | ('redirect_301', url) | ('gone', 410)
    A 301 requires an explicitly approved, live, equivalent successor. 410 is
    only for ARCHIVED with no live replacement. Nothing here is auto-enabled;
    the PDP view opts in behind a flag."""
    st = lifecycle_status(product)
    if st in ("ACTIVE", "OUT_OF_STOCK", "SEASONAL"):
        return ("render", 200)
    if st == "DISCONTINUED":
        if (product.get("replacement_product_id")
                and int(product.get("redirect_approved") or 0) == 1
                and replacement_is_live(replacement)):
            return ("redirect_301", replacement)
        return ("render_unavailable", 200)
    # ARCHIVED
    if (product.get("replacement_product_id")
            and int(product.get("redirect_approved") or 0) == 1
            and replacement_is_live(replacement)):
        return ("redirect_301", replacement)
    return ("gone", 410)


def schema_availability(product):
    """schema.org availability URL matching the lifecycle state (feed/PDP must
    agree). Google supports InStock / OutOfStock / BackOrder / Discontinued."""
    st = lifecycle_status(product)
    if st == "DISCONTINUED":
        return "https://schema.org/Discontinued"
    if st in ("OUT_OF_STOCK", "SEASONAL", "ARCHIVED"):
        return "https://schema.org/OutOfStock"
    return "https://schema.org/InStock" if _qty_int(product) > 0 else "https://schema.org/OutOfStock"


_DIM_CACHE = {}


def image_dimensions(path):
    """Real (width, height) in pixels of a master image, read from the file
    header once and cached. Returns None if unavailable. Never fabricated."""
    rel = img_static_relpath(path)
    if not rel:
        return None
    if rel in _DIM_CACHE:
        return _DIM_CACHE[rel]
    dim = None
    try:
        from PIL import Image
        fs = os.path.join(current_app.static_folder, rel[len("/static/"):])
        with Image.open(fs) as im:
            dim = im.size  # header-only; does not decode pixels
    except Exception:
        dim = None
    _DIM_CACHE[rel] = dim
    return dim


def versioned_angle_urls(product_image_str, base_url="", limit=None):
    """Deduped, ordered list of versioned MASTER image URLs for a product.
    The catalog stores some masters twice (e.g. /CODE/ and /opticalbazaar/CODE/);
    byte-identical files share a ?v= hash, so we key on that (fallback: the URL)
    to avoid duplicate images in feeds, sitemaps and the media API."""
    if not product_image_str:
        return []
    seen, out = set(), []
    for entry in product_image_str.split(","):
        u = versioned_image_url(entry, base_url)
        if not u:
            continue
        k = u.split("?v=", 1)[1] if "?v=" in u else u
        if k in seen:
            continue
        seen.add(k)
        out.append(u)
        if limit is not None and len(out) >= limit:
            break
    return out


def build_media_one(path):
    """Structured media object for ONE image entry. Same derivative-URL logic
    as the Jinja macro, produced server-side so JS never rebuilds filenames.
    Returns None for empty input. Shape:
      {src, zoom, has_derivatives, avif, webp, jpg}
    where avif/webp/jpg are width-descriptor srcset strings ('' when no ladder).
    Callers/JS apply surface-specific sizes/width/height/loading."""
    p = (path or "").strip()
    if not p:
        return None
    src = _ver_url(p)
    media = {"src": src, "zoom": src, "has_derivatives": False,
             "avif": "", "webp": "", "jpg": ""}
    if img_has_derivatives(p):
        d, _, fname = p.rpartition("/")
        stem = fname.rsplit(".", 1)[0]
        base = (d + "/" if d else "") + "derivatives/" + stem

        def _srcset(fmt):
            return ", ".join(
                "%s %dw" % (_ver_url("%s-%d.%s" % (base, w, fmt)), w)
                for w in DERIVATIVE_WIDTHS
            )

        media["has_derivatives"] = True
        media["avif"] = _srcset("avif")
        media["webp"] = _srcset("webp")
        media["jpg"] = _srcset("jpg")
    return media


def build_media_list(product_image_str, limit=None):
    """List of media objects for a comma-separated product_image string.
    `limit` caps the number of angles (use limit=1 for card/list APIs that
    display only the primary image and no gallery — keeps payloads small)."""
    if not product_image_str:
        return []
    out = []
    for entry in product_image_str.split(","):
        m = build_media_one(entry)
        if m:
            out.append(m)
        if limit is not None and len(out) >= limit:
            break
    return out


def build_media_primary(product_image_str):
    """Single media object for the FIRST (primary) image only, or None.
    For list/card/recommendation surfaces that never show a gallery."""
    lst = build_media_list(product_image_str, limit=1)
    return lst[0] if lst else None


# Frame-shape flags (product_category_*) in priority order -> schema.org label.
# rimless/semirimless/supra are rim TYPES, not shapes, so excluded here.
_SHAPE_FLAGS = (
    ("product_category_rectangle", "Rectangle"),
    ("product_category_wayfarer", "Wayfarer"),
    ("product_category_aviator", "Aviator"),
    ("product_category_cateye", "Cat Eye"),
    ("product_category_round", "Round"),
    ("product_category_oval", "Oval"),
    ("product_category_square", "Square"),
    ("product_category_clubmaster", "Clubmaster"),
    ("product_category_browline", "Browline"),
    ("product_category_panto", "Panto"),
    ("product_category_quatra", "Quatra"),
    ("product_category_horn", "Horn-Rimmed"),
    ("product_category_oversized", "Oversized"),
)


def frame_shape(product):
    """Human/schema-friendly frame shape derived from the product_category_*
    flags (product_shape column is unpopulated). Returns '' if none set."""
    if not product:
        return ""
    for flag, label in _SHAPE_FLAGS:
        try:
            if int(product.get(flag) or 0) == 1:
                return label
        except (TypeError, ValueError):
            continue
    return ""


def register_image_helpers(app):
    _load(app)
    app.jinja_env.globals["img_has_derivatives"] = img_has_derivatives
    app.jinja_env.globals["img_ver"] = img_ver
    app.jinja_env.globals["build_media_list"] = build_media_list
    app.jinja_env.globals["build_media_primary"] = build_media_primary
    app.jinja_env.globals["versioned_image_url"] = versioned_image_url
    app.jinja_env.globals["frame_shape"] = frame_shape
    app.jinja_env.globals["image_dimensions"] = image_dimensions
