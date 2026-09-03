"""An owner's look at a lens the storefront does not yet sell.

A lens at ``merchant_enabled=0`` has no page: that is the release gate, and
it is right. But the person who decides whether to open the gate has to see
the page first, on the real storefront, with the real row, and the only way to
show it to them without showing it to everybody is a link only they hold.

The link carries a token: an expiry and an HMAC over the product id and that
expiry, keyed with a secret only the server knows. Presenting it once grants
the browser session a preview of that one product until the token expires.
Nothing else opens: the preview never applies on optiwar.in, never lifts any
blocker but ``merchant_enabled=0`` (a lens with no images or no rules stays
DRAFT for the owner too), and a previewed page is sent ``noindex``. Issuing a
link is an operator act, done on the box with the server's own secret.
"""
import hashlib
import hmac
import time

SESSION_KEY = "lens_preview"
NOT_RELEASED = "merchant_enabled=0"
MAX_HOURS = 24 * 14
_DEFAULT_SECRET_KEY = "#4418@1220042ksk$dkdk%sdskl!!"


def secret(environ):
    """The signing key, or ``None`` when the server has no real secret.

    ``LENS_PREVIEW_SECRET`` when set, else the Flask ``SECRET_KEY`` — but never
    the development default baked into ``create_app``, which anyone with the
    repository could use to sign themselves a preview of every draft.
    """
    value = environ.get("LENS_PREVIEW_SECRET") or environ.get("SECRET_KEY")
    if not value or value == _DEFAULT_SECRET_KEY:
        return None
    return value


def _sign(key, product_id, expires):
    message = "lens-preview|%s|%d" % (int(product_id), int(expires))
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def issue(key, product_id, hours, now=None):
    """A token valid for ``hours`` from ``now``, as ``<expires>.<signature>``."""
    if not key:
        raise ValueError("no preview secret configured")
    hours = float(hours)
    if not 0 < hours <= MAX_HOURS:
        raise ValueError("preview validity must be within 0-%d hours"
                         % MAX_HOURS)
    expires = int((now if now is not None else time.time()) + hours * 3600)
    return "%d.%s" % (expires, _sign(key, product_id, expires))


def verify(key, product_id, token, now=None):
    """The token's expiry when it is genuine for this product and unexpired."""
    if not key or not token or product_id is None:
        return None
    expires, _, signature = str(token).partition(".")
    if not (expires.isdigit() and signature):
        return None
    try:
        pid = int(product_id)
    except (TypeError, ValueError):
        return None
    expires = int(expires)
    if expires <= (now if now is not None else time.time()):
        return None
    if not hmac.compare_digest(_sign(key, pid, expires), signature):
        return None
    return expires


def grant(session, product_id, expires):
    """Remember, in this browser session, that this product may be previewed."""
    grants = dict(session.get(SESSION_KEY) or {})
    grants[str(int(product_id))] = int(expires)
    session[SESSION_KEY] = grants


def granted(session, product_id, now=None):
    """True while this session holds an unexpired preview of this product."""
    grants = session.get(SESSION_KEY) or {}
    expires = grants.get(str(product_id))
    try:
        return int(expires or 0) > (now if now is not None else time.time())
    except (TypeError, ValueError):
        return False


def previewable(row):
    """A lens the owner may preview: everything passes but the release flag."""
    return tuple(row.get("release_blockers") or ()) == (NOT_RELEASED,)
