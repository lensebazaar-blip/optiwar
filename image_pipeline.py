"""Deterministic product-image pipeline: source photograph -> master -> web.

Three levels, one direction:

    source/   the photograph as the camera wrote it. Never modified, never an
              input to a second pass.
    master/   one square sRGB commerce image per view, cut from a source
              photograph by a recipe that is versioned next to this code.
    web/      the responsive ladder (AVIF/WebP/JPEG at DERIVATIVE_WIDTHS),
              generated from the master, never edited by hand.

Every operation here is deterministic and stated in the recipe: a rectangle in
source pixels, an exact rotation, a contrast cutoff, a canvas colour. Nothing
invents pixels — no upscaling beyond the source, no generative model, no
"enhancement" that could rewrite package text, a logo or a regulatory symbol on
a medical device. Running the same recipe over the same photographs twice
produces byte-identical masters, which is what makes the whole image system
transferable to another server: the photographs plus this file plus the recipe
reproduce every derivative.

A recipe (see ``image_recipes/``) is JSON::

    {
      "product": "PRECISION1",
      "catalog_dir": "contact-lenses/PRECISION1",
      "master_px": 2000,
      "views": [
        {"code": "01_hero", "view": "front", "source": "IMG_2361.JPG",
         "crop": [x, y, w, h], "rotate": 0, "alt": "...", "primary": true}
      ],
      "missing_views": [{"code": "02_angle", "reason": "..."}]
    }

``crop`` is in source pixels; the cut is padded to a square canvas on
``background`` and resized to ``master_px``. A view whose photograph does not
exist is declared in ``missing_views`` rather than fabricated, and the caller
reports it as a QA warning.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

from embed_helper import DERIVATIVE_FORMATS, DERIVATIVE_WIDTHS

# JPEG quality for a master (visually lossless at 2000px) and for the ladder.
MASTER_QUALITY = 92
WEB_QUALITY = 82
WHITE = (255, 255, 255)

REQUIRED_VIEW_KEYS = ("code", "view", "source", "alt")


class RecipeError(ValueError):
    """The recipe does not describe something that can be built."""


def load_recipe(path):
    with open(path, encoding="utf-8") as fh:
        recipe = json.load(fh)
    return validate_recipe(recipe)


def validate_recipe(recipe):
    for key in ("product", "catalog_dir", "views"):
        if not recipe.get(key):
            raise RecipeError("recipe has no %s" % key)
    codes = set()
    primaries = 0
    for view in recipe["views"]:
        for key in REQUIRED_VIEW_KEYS:
            if not view.get(key):
                raise RecipeError("view %s has no %s"
                                  % (view.get("code", "?"), key))
        if view["code"] in codes:
            raise RecipeError("view %s stated twice" % view["code"])
        codes.add(view["code"])
        crop = view.get("crop")
        if crop is not None and (len(crop) != 4 or min(crop[2:]) <= 0):
            raise RecipeError("view %s has an unusable crop" % view["code"])
        primaries += 1 if view.get("primary") else 0
    if primaries != 1:
        raise RecipeError("a product has exactly one primary image, not %d"
                          % primaries)
    recipe.setdefault("master_px", 2000)
    recipe.setdefault("background", list(WHITE))
    recipe.setdefault("missing_views", [])
    return recipe


# ---------------------------------------------------------------------------
# master
# ---------------------------------------------------------------------------

def build_master(image, view, master_px=2000, background=WHITE):
    """One square commerce master from one source photograph.

    Order is fixed so the result cannot depend on how a recipe was written:
    EXIF orientation, crop, rotation, background removal, contrast, square
    canvas, resize.
    """
    img = ImageOps.exif_transpose(image)
    if img.mode != "RGB":
        img = img.convert("RGB")
    crop = view.get("crop")
    if crop:
        x, y, w, h = (int(v) for v in crop)
        img = img.crop((x, y, x + w, y + h))
    rotate = float(view.get("rotate") or 0) % 360
    if rotate in (90.0, 180.0, 270.0):
        # A quarter turn is a transpose: no resampling, no pixel invented.
        img = img.transpose({90.0: Image.ROTATE_270,
                             180.0: Image.ROTATE_180,
                             270.0: Image.ROTATE_90}[rotate])
    elif rotate:
        img = img.rotate(-rotate, resample=Image.BICUBIC, expand=True,
                         fillcolor=tuple(background))
    isolate = view.get("isolate")
    if isolate:
        options = isolate if isinstance(isolate, dict) else {}
        img = isolate_product(img,
                              options.get("border", 0.06),
                              options.get("distance", 6.0),
                              options.get("clean", 9),
                              options.get("colour", 0.45),
                              options.get("bright", 1.15),
                              options.get("feather", 2),
                              options.get("shrink", 0))
    cutoff = view.get("autocontrast")
    if cutoff:
        img = ImageOps.autocontrast(img, cutoff=float(cutoff))
    img = square_canvas(img, background, view.get("margin", 0.04))
    if img.size != (master_px, master_px):
        # Never enlarge past the source: a master smaller than the target is
        # left at its own size rather than upscaled into invented detail.
        target = min(master_px, max(img.size))
        img = img.resize((target, target), Image.LANCZOS)
    return img


def product_hull(img, border=0.06, distance=6.0, clean=9, colour=0.45,
                 bright=1.15, coarse=16):
    """The carton's outline, as a convex polygon in image coordinates.

    Nothing about the product is assumed. The photograph's four corners are a
    sample of the surface it was shot on -- a centred product reaches an edge
    long before it reaches a corner -- so the surface's colour distribution is
    measured there and every pixel far from that distribution (Mahalanobis
    distance above ``distance``) is a product candidate. Speckle in the wood
    grain is removed by an erode/dilate pair, the largest remaining region is
    kept, and its convex hull becomes the outline -- a carton is convex, so the
    hull recovers the white panels and pale print that colour alone misses.

    Returns a list of (x, y) vertices, or None when no region was found.
    """
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    height, width = arr.shape[:2]
    edge = max(4, int(round(min(height, width) * float(border))))
    frame = np.concatenate([arr[:edge, :edge].reshape(-1, 3),
                            arr[:edge, -edge:].reshape(-1, 3),
                            arr[-edge:, :edge].reshape(-1, 3),
                            arr[-edge:, -edge:].reshape(-1, 3)])
    mean = frame.mean(axis=0)
    cov = np.cov(frame, rowvar=False) + np.eye(3) * 1e-3
    delta = arr.reshape(-1, 3) - mean
    md = np.sqrt(np.einsum("ij,jk,ik->i", delta, np.linalg.inv(cov), delta))
    md = md.reshape(height, width)

    # Being unlike the surface is not enough: the product's own shadow is also
    # unlike it. Printed carton is either colourful or brighter than the
    # surface, and a shadow is neither, so it stays background.
    weights = np.array([0.299, 0.587, 0.114], dtype=np.float32)
    luma = arr @ weights
    high = arr.max(axis=2)
    chroma = (high - arr.min(axis=2)) / np.maximum(high, 1.0)
    surface_luma = float(mean @ weights)
    mask = (md > float(distance)) & ((chroma > float(colour))
                                     | (luma > surface_luma * float(bright)))

    if clean:
        image = Image.fromarray((mask * 255).astype(np.uint8))
        image = image.filter(ImageFilter.MinFilter(clean))
        image = image.filter(ImageFilter.MaxFilter(clean))
        mask = np.asarray(image) > 127

    # Region labelling runs on a 1/coarse copy for speed, but the hull is then
    # taken from the full-resolution mask inside that region, so the outline
    # hugs the carton instead of the coarse grid.
    small = np.asarray(Image.fromarray((mask * 255).astype(np.uint8)).resize(
        (max(1, width // coarse), max(1, height // coarse)), Image.NEAREST)) > 127
    region = _largest_region(small)
    if region is None:
        return None
    grown = np.asarray(Image.fromarray((region * 255).astype(np.uint8)).resize(
        (width, height), Image.NEAREST)) > 127
    ys, xs = np.where(mask & grown)
    if not len(xs):
        return None
    return _convex_hull(np.stack([xs, ys], axis=1))


def _largest_region(mask):
    """The largest 4-connected region of a small boolean mask."""
    labels = np.zeros(mask.shape, dtype=np.int32)
    best, best_size, label = None, 0, 0
    for seed in zip(*np.where(mask)):
        if labels[seed]:
            continue
        label += 1
        stack, size = [seed], 0
        labels[seed] = label
        while stack:
            y, x = stack.pop()
            size += 1
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if (0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1]
                        and mask[ny, nx] and not labels[ny, nx]):
                    labels[ny, nx] = label
                    stack.append((ny, nx))
        if size > best_size:
            best, best_size = label, size
    if best is None:
        return None
    return labels == best


def _convex_hull(points):
    """Monotone chain hull, clockwise, as a list of (x, y) tuples."""
    pts = sorted({(int(x), int(y)) for x, y in points})
    if len(pts) < 3:
        return pts

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2 and _cross(out[-2], out[-1], p) <= 0:
                out.pop()
            out.append(p)
        return out

    return half(pts)[:-1] + half(reversed(pts))[:-1]


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def isolate_product(img, border=0.06, distance=6.0, clean=9, colour=0.45,
                    bright=1.15, feather=2, shrink=0):
    """Replace everything outside the carton's outline with the canvas colour.

    Returns the image cropped to the outline's bounding box. The edge is
    feathered so the carton does not acquire a hard cut-out fringe.
    """
    hull = product_hull(img, border, distance, clean, colour, bright)
    if not hull:
        return img
    alpha = Image.new("L", img.size, 0)
    ImageDraw.Draw(alpha).polygon(hull, fill=255)
    if shrink:
        alpha = alpha.filter(ImageFilter.MinFilter(int(shrink)))
    if feather:
        alpha = alpha.filter(ImageFilter.GaussianBlur(float(feather)))
    white = Image.new("RGB", img.size, WHITE)
    flat = Image.composite(img.convert("RGB"), white, alpha)
    xs = [p[0] for p in hull]
    ys = [p[1] for p in hull]
    return flat.crop((max(0, min(xs)), max(0, min(ys)),
                      min(img.size[0], max(xs) + 1),
                      min(img.size[1], max(ys) + 1)))


def square_canvas(img, background=WHITE, margin=0.04):
    """Centre the cut on a square canvas with a fixed margin, no crop loss."""
    side = int(round(max(img.size) * (1 + 2 * float(margin))))
    canvas = Image.new("RGB", (side, side), tuple(background))
    canvas.paste(img, ((side - img.size[0]) // 2, (side - img.size[1]) // 2))
    return canvas


def save_master(img, path):
    _ensure_dir(os.path.dirname(path))
    _atomic_write(path, lambda fh: img.save(
        fh, "JPEG", quality=MASTER_QUALITY, subsampling=0, optimize=True,
        progressive=True, icc_profile=None))
    return path


# ---------------------------------------------------------------------------
# web derivatives
# ---------------------------------------------------------------------------

def build_derivatives(master_path, widths=DERIVATIVE_WIDTHS,
                      formats=DERIVATIVE_FORMATS):
    """Write <dir>/derivatives/<stem>-<width>.<fmt>; return the paths written.

    A format the box cannot encode is skipped, not faked: the <picture> element
    falls back AVIF -> WebP -> JPEG, and JPEG is always produced, so a customer
    never gets a broken image because libavif is missing.
    """
    directory, filename = os.path.split(master_path)
    stem = os.path.splitext(filename)[0]
    out_dir = os.path.join(directory, "derivatives")
    _ensure_dir(out_dir)
    written = []
    with Image.open(master_path) as master:
        master.load()
        for width in widths:
            if width > master.size[0]:
                # No upscaling: a ladder rung wider than the master would be
                # invented pixels. The srcset simply stops at the master.
                continue
            resized = master.resize(
                (width, int(round(master.size[1] * width / master.size[0]))),
                Image.LANCZOS)
            for fmt in formats:
                path = os.path.join(out_dir, "%s-%d.%s" % (stem, width, fmt))
                if _save_format(resized, path, fmt):
                    written.append(path)
    return written


def _save_format(img, path, fmt):
    if fmt == "jpg":
        _atomic_write(path, lambda fh: img.save(
            fh, "JPEG", quality=WEB_QUALITY, optimize=True, progressive=True))
        return True
    if fmt == "webp":
        _atomic_write(path, lambda fh: img.save(fh, "WEBP", quality=WEB_QUALITY,
                                                method=6))
        return True
    if fmt == "avif":
        return _save_avif(img, path)
    raise RecipeError("unknown derivative format %r" % fmt)


def _save_avif(img, path):
    """Pillow's AVIF plugin when the box has one, else the avifenc binary."""
    try:
        _atomic_write(path, lambda fh: img.save(fh, "AVIF", quality=WEB_QUALITY))
        return True
    except (KeyError, OSError, ValueError):
        pass
    binary = shutil.which("avifenc")
    if not binary:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "in.png")
        img.save(png, "PNG")
        out = os.path.join(tmp, "out.avif")
        result = subprocess.run(
            [binary, "--min", "0", "--max", "40", "-a", "end-usage=q",
             "-a", "cq-level=28", "-s", "6", png, out],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if result.returncode != 0 or not os.path.exists(out):
            return False
        _ensure_dir(os.path.dirname(path))
        shutil.copyfile(out, path)
    return True


# ---------------------------------------------------------------------------
# manifest + records
# ---------------------------------------------------------------------------

def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_rows(paths, catalog_root):
    """(catalog-relative path, sha256) for embed_helper's manifest, sorted."""
    rows = []
    for path in paths:
        rel = os.path.relpath(path, catalog_root).replace(os.sep, "/")
        rows.append((rel, sha256(path)))
    return sorted(rows)


def merge_manifest(manifest_path, rows):
    """Rewrite the manifest with `rows` replacing any entry of the same path.

    An entry is replaced rather than appended so a re-run cannot leave two
    hashes for one file, and the file stays sorted so a diff is readable.
    """
    existing = {}
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            for line in fh:
                rel, _, digest = line.rstrip("\n").partition("\t")
                if rel and digest:
                    existing[rel] = digest
    existing.update(dict(rows))
    _ensure_dir(os.path.dirname(manifest_path))
    _atomic_write(manifest_path, lambda fh: fh.write("".join(
        "%s\t%s\n" % (rel, existing[rel]) for rel in sorted(existing)
    ).encode("utf-8")))
    return len(existing)


def image_records(recipe):
    """The approved images as the catalogue and the passport describe them.

    One list, consumed by the importer (contact_lens_images), the PDP gallery,
    JSON-LD, the image sitemap and the merchant feed, so no surface invents an
    image URL of its own.
    """
    records = []
    for position, view in enumerate(recipe["views"], start=1):
        records.append({
            "view": view["view"],
            "code": view["code"],
            "position": position,
            "is_primary": bool(view.get("primary")),
            "path": "%s/%s.jpg" % (recipe["catalog_dir"], view["code"]),
            "source": "OPTIWAR_ORIGINAL_PHOTOGRAPH",
            "processing": "DETERMINISTIC_APPROVED",
            "source_file": view["source"],
            "alt": view["alt"],
            "gmc": bool(view.get("gmc", True)),
        })
    records.sort(key=lambda r: (not r["is_primary"], r["position"]))
    return records


def qa_warnings(recipe, records):
    """Release-blocking and advisory findings, in that order."""
    warnings = []
    if not any(r["is_primary"] for r in records):
        warnings.append(("BLOCK", "no primary image"))
    for missing in recipe.get("missing_views", []):
        warnings.append(("WARN", "view %s not photographed: %s"
                         % (missing.get("code"), missing.get("reason", ""))))
    return warnings


# ---------------------------------------------------------------------------

def build_product(recipe, source_dir, catalog_root, apply=False):
    """Build every master and its ladder. Returns (records, written, warnings).

    ``apply=False`` reports what would be written and touches nothing.
    """
    records = image_records(recipe)
    warnings = qa_warnings(recipe, records)
    written = []
    for view in recipe["views"]:
        src = os.path.join(source_dir, view["source"])
        if not os.path.exists(src):
            warnings.append(("BLOCK", "source photograph missing: %s"
                             % view["source"]))
            continue
        master_path = os.path.join(catalog_root, recipe["catalog_dir"],
                                   "%s.jpg" % view["code"])
        if not apply:
            written.append(master_path)
            continue
        with Image.open(src) as image:
            master = build_master(image, view, recipe["master_px"],
                                  tuple(recipe["background"]))
        written.append(save_master(master, master_path))
        written.extend(build_derivatives(master_path))
    return records, written, warnings


def _ensure_dir(path):
    if path:
        os.makedirs(path, exist_ok=True)


def _atomic_write(path, write):
    """Write through a temporary file so a crash cannot leave a half image."""
    _ensure_dir(os.path.dirname(path))
    buf = io.BytesIO()
    write(buf)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(buf.getvalue())
    os.replace(tmp, path)
