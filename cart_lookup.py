"""LOOKUP_CART: what is in this browser's cart, read from the server session.

Item names and quantities only. Prices, discounts, lens charges and the total
come from the cart page's own calculation, so the assistant names the page
instead of adding anything up.
"""
import re
from urllib.parse import urlparse

CART_RE = re.compile(r"\b(cart|basket|kart|carrito|panier|warenkorb)\b|"
                     "\u0915\u093e\u0930\u094d\u091f", re.IGNORECASE)
CART_PAGES = ("/cart", "/checkout")


def wanted(message, page_url):
    """The customer asks about the cart, or is on the cart or checkout page."""
    path = urlparse(page_url or "").path.rstrip("/")
    return path in CART_PAGES or bool(CART_RE.search(message or ""))


def _quantity(item):
    eyes = int(item.get("right_qty") or 0) + int(item.get("left_qty") or 0)
    return eyes or int(item.get("order_quantity") or 1)


def read_model(cart):
    return {"items": [{"name": str(i.get("product_name") or i.get("product_code") or "item"),
                       "quantity": _quantity(i)}
                      for i in cart or () if isinstance(i, dict)]}


def found(model):
    return bool(model and model.get("items"))


def prompt_section(model):
    lines = ["", "CART (LOOKUP_CART: read just now from this browser's cart; authoritative):"]
    for i in (model or {}).get("items") or ():
        lines.append("  %s × %d" % (i["name"], i["quantity"]))
    if not found(model):
        lines.append("  The cart is empty.")
    lines.append("  Name only these items and quantities. Never state a price, discount or "
                 "total for the cart: the cart page (/cart) shows them. You cannot add, remove "
                 "or change items; tell the customer to do it on /cart.")
    return "\n".join(lines) + "\n"


def event_payload(model):
    """Counts only, no product names."""
    items = (model or {}).get("items") or ()
    return {"lines": len(items), "units": sum(i["quantity"] for i in items)}
