"""A customer's chat photo, re-encoded small before Optiwar keeps or forwards it.

Every accepted photo is decoded and saved again as one baseline JPEG: at most
``MAX_EDGE`` px on its long side, turned the way the camera held it,
transparency flattened onto white, and nothing of the original file carried
over (no EXIF, so no location or camera serial, no trailing bytes). The
result is never over ``SEND_MAX_BYTES``, which is what KET and the vision
model receive.
"""
import io

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_EDGE = 2048
SEND_MAX_BYTES = 5 * 1024 * 1024
MAX_DECODED_PIXELS = 40_000_000
QUALITIES = (82, 72, 62, 50)
MIME = "image/jpeg"


class Unreadable(Exception):
    """The bytes look like a photo but do not decode as one."""


class TooLarge(Exception):
    """Even the smallest re-encoding is over ``SEND_MAX_BYTES``."""


def _flatten(img):
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        return bg
    return img.convert("RGB")


def shrink(data):
    """``data`` as the smallest-sensible JPEG bytes; raises Unreadable/TooLarge."""
    try:
        with Image.open(io.BytesIO(data)) as src:
            src.draft("RGB", (MAX_EDGE, MAX_EDGE))
            if src.width * src.height > MAX_DECODED_PIXELS:
                raise Unreadable("too many pixels")
            src.load()
            try:
                img = ImageOps.exif_transpose(src)
            except Exception:  # noqa: BLE001 - a broken EXIF block only loses the turn
                img = src
            img = _flatten(img)
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            for quality in QUALITIES:
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
                out = buf.getvalue()
                if len(out) <= SEND_MAX_BYTES:
                    return out
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError,
            ValueError, SyntaxError) as e:
        raise Unreadable(str(e)) from e
    raise TooLarge("over %d bytes" % SEND_MAX_BYTES)
