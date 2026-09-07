"""The cart-level contact-lens rule: minimums, and the eyewear that waives them.

On optiwar.com a lens's stated minimum boxes (lens_order.minimums) is waived
when the eligible eyewear in the same cart comes to EUR 35 or more:

    eligible_eyewear_subtotal >= 35 EUR      -> lens minimums waived
    anything less                             -> per-product minimums apply

"Eligible eyewear" is the spectacle-frame lines, at what they are charged —
frame plus its lenses, after the two-frame discount checkout applies. It is
not the cart total and not the lens total, and it is never read from a flag
the browser sent: every function here recomputes it from the cart as held in
the session, so the same answer comes back on add, remove, quantity change,
restore and checkout.

``revalidate()`` is the one function every mutation calls: it returns the
cart with any lens line that no longer meets its minimum removed, and the
lines it removed, so the caller can make one atomic write and say what went.
``would_remove()`` is the same check run before a mutation is committed, so
the customer can be asked first.
"""
import decimal

try:
    from . import lens_order
except ImportError:
    import lens_order

WAIVER_THRESHOLD_EUR = decimal.Decimal("35")
TWO_FRAME_DISCOUNT_EUR = decimal.Decimal("15")
SITE_COM = "optiwar.com"

CONFIRM_REMOVAL = ("Removing this eyewear item will also remove the contact "
                   "lenses that were added under the eyewear order benefit.")


def _money(value):
    try:
        return decimal.Decimal(str(value if value is not None else 0))
    except decimal.InvalidOperation:
        return decimal.Decimal(0)


def _count(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def is_lens(item):
    return (item.get("vertical") == "CONTACT_LENS"
            or (str(item.get("product_category") or "").strip().lower()
                == "contact lenses"
                and ("right_qty" in item or "left_qty" in item)))


def is_eligible_eyewear(item):
    """The same test checkout's two-frame discount uses."""
    return "Spectacles Frame" in str(item.get("product_category") or "")


def line_total(item):
    """What a cart line is charged, in the shape checkout sums it."""
    total = (_money(item.get("ATC_total")) + _money(item.get("server_total_price"))
             + _money(item.get("ATC_WCL")))
    if total == 0:
        total = (_money(item.get("product_special_price"))
                 * _count(item.get("order_quantity")) or decimal.Decimal(0))
    return total


def eyewear_subtotal(cart, site=SITE_COM):
    """The eligible eyewear subtotal in EUR; zero on any other storefront.

    The two-frame discount is subtracted because it changes what the eyewear
    costs, and the rule is about eyewear spend, not list price.
    """
    if site != SITE_COM:
        return decimal.Decimal(0)
    frames = [i for i in cart if is_eligible_eyewear(i)]
    subtotal = sum((line_total(i) for i in frames), decimal.Decimal(0))
    if len(frames) >= 2:
        subtotal -= TWO_FRAME_DISCOUNT_EUR
    return max(subtotal, decimal.Decimal(0))


def minimums_waived(cart, site=SITE_COM):
    return eyewear_subtotal(cart, site) >= WAIVER_THRESHOLD_EUR


def lens_lines(item):
    """``[{"eye", "boxes"}]`` for the eyes this lens line orders."""
    return [{"eye": eye, "boxes": _count(item.get("%s_qty" % eye))}
            for eye in lens_order.EYES if _count(item.get("%s_qty" % eye))]


def below_minimum(item, product, site=SITE_COM, waived=False):
    """Whether this lens line fails its product's minimum, eye by eye."""
    lines = lens_lines(item)
    if not lines:
        return False
    return bool(lens_order._minimum_problems(product, lines, site, waived))


def revalidate(cart, products, site=SITE_COM):
    """``(kept, removed)``: the cart with every lens line that no longer meets
    its minimum taken out.

    ``products`` maps product_id (as text) to the profile row that carries
    ``min_boxes_single_eye`` / ``min_boxes_both_per_eye``. A lens whose row is
    unknown is kept: an absent product is not a below-minimum one, and the
    checkout's own eligibility check is where a vanished product is refused.
    The waiver is decided from this cart, then applied to every lens in it.
    """
    waived = minimums_waived(cart, site)
    kept, removed = [], []
    for item in cart:
        if not is_lens(item):
            kept.append(item)
            continue
        product = products.get(str(item.get("product_id")))
        if product is not None and below_minimum(item, product, site, waived):
            removed.append(item)
        else:
            kept.append(item)
    return kept, removed


def would_remove(cart_after, products, site=SITE_COM):
    """The lens lines a proposed cart would lose to the minimum rule."""
    return revalidate(cart_after, products, site)[1]


def lens_product_ids(cart):
    return sorted({str(i.get("product_id")) for i in cart if is_lens(i)})


def load_products(cursor, cart):
    """The minimum columns for every lens in the cart, keyed by product_id."""
    ids = lens_product_ids(cart)
    if not ids:
        return {}
    marks = ", ".join(["%s"] * len(ids))
    cursor.execute("SELECT product_id, min_boxes_single_eye, "
                   "min_boxes_both_per_eye FROM contact_lens_products "
                   "WHERE product_id IN (%s)" % marks, tuple(ids))
    return {str(r["product_id"]): r for r in cursor.fetchall()}


def describe_removed(removed):
    """One sentence naming what was taken out, for a flash message."""
    names = []
    for item in removed:
        boxes = sum(ln["boxes"] for ln in lens_lines(item))
        names.append("%s (%d box%s)" % (item.get("product_name") or "lens",
                                        boxes, "" if boxes == 1 else "es"))
    return ("Removed from your cart because the eyewear order benefit no "
            "longer applies and the minimum boxes are not met: %s."
            % "; ".join(names))
