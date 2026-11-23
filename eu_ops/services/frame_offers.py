"""Ops client and customer-copy rendering for Optiwar frame offers."""

import html
import os
import re
from urllib.parse import quote

import requests

from services import reship


TIMEOUT = 8
EVENT = "ow_frame_qc_offer"
WA_TEMPLATE_NAME = "ow_frame_qc_replace"
WA_BODY = (
    "Hello {{1}}, about your Optiwar order {{2}}: the frame you chose, {{3}}, "
    "did not pass our quality check, so we won't send it. We have similar "
    "frames in stock at no extra charge. Please choose a replacement, or ask "
    "for a full refund, here: {{4}}\n\nThe link works for 7 days."
)


def api_call(path, method="GET", payload=None):
    token = reship._token()
    if not token:
        return False, "OPS_API_TOKEN not set"
    try:
        response = requests.request(
            method, reship._base() + path,
            headers={"Authorization": "Bearer %s" % token,
                     "Accept": "application/json"},
            json=payload, timeout=TIMEOUT)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code not in (200, 201):
            return False, str(data.get("message") or data.get("error")
                              or "HTTP %s" % response.status_code)
        return True, data
    except Exception as exc:
        return False, str(exc)


def matches(order_id, order_line_id):
    return api_call("/ops/api/orders/%s/frame-matches?order_line_id=%s" %
                    (quote(str(order_id), safe=""), quote(str(order_line_id), safe="")))


def latest(order_id, order_line_id=None):
    path = "/ops/api/orders/%s/frame-offer" % quote(str(order_id), safe="")
    if order_line_id is not None:
        path += "?order_line_id=%s" % quote(str(order_line_id), safe="")
    ok, data = api_call(path)
    if not ok:
        return False, data
    rows = data.get("offers") or []
    return True, rows[0] if rows else None


def create(order_id, order_line_id, reason, product_ids, created_by):
    return api_call("/ops/api/orders/%s/frame-offer" %
                    quote(str(order_id), safe=""), "POST", {
                        "order_line_id": int(order_line_id), "reason": reason,
                        "product_ids": [int(pid) for pid in product_ids],
                        "created_by": created_by,
                    })


def settled(order_id, offer_uuid, ref, by):
    return api_call("/ops/api/orders/%s/frame-offer/%s/settled" %
                    (quote(str(order_id), safe=""), quote(str(offer_uuid), safe="")),
                    "POST", {"ref": ref, "by": by})


def open_offers():
    return api_call("/ops/api/frame-offers/open")


def reminder_link(offer_uuid):
    return api_call("/ops/api/frame-offers/%s/link" % quote(str(offer_uuid), safe=""))


def mark(offer_uuid, action):
    return api_call("/ops/api/frame-offers/%s/mark" % quote(str(offer_uuid), safe=""),
                    "POST", {"action": action})


def wa_template():
    value = (os.getenv("OW_FRAME_QC_WA_TEMPLATE") or "").strip()
    return value if value == WA_TEMPLATE_NAME else ""


def send_gate(reason, template=None):
    if str(reason or "").upper() == "OUT_OF_STOCK":
        return False, "Needs its own approved wording/template"
    selected = wa_template() if template is None else str(template or "").strip()
    if selected != WA_TEMPLATE_NAME:
        return False, "Waiting for msg91 approval of ow_frame_qc_replace"
    return True, ""


def money(minor):
    value = str(abs(int(minor or 0)) // 100)
    if len(value) > 3:
        tail, head = value[-3:], value[:-3]
        groups = []
        while head:
            groups.insert(0, head[-2:])
            head = head[:-2]
        value = ",".join(groups + [tail])
    return ("-₹" if int(minor or 0) < 0 else "₹") + value


def first_name(name):
    return (str(name or "").strip().split() or ["there"])[0]


def email_subject(order_id):
    return "Your Optiwar order OW-%s: please choose a replacement frame" % order_id


def customer_email(order, failed, candidates, link):
    any_payment = any(
        int(c["price_minor"]) - int(failed["price_minor"]) >= 5000
        for c in candidates)
    rows = []
    for candidate in candidates:
        diff = int(candidate["price_minor"]) - int(failed["price_minor"])
        price = ("Pay %s more" % money(diff) if diff >= 5000
                 else "No extra charge")
        image = ('<img src="%s" alt="" width="120" style="display:block;">'
                 % html.escape(candidate.get("image_url") or "", quote=True)
                 if candidate.get("image_url") else "")
        link_html = html.escape(link, quote=True)
        rows.append(
            '<td style="padding:12px;border:1px solid #ddd;">%s'
            '<strong>%s · %s</strong><br>%s · %s<br>%s</td>' %
            (image, html.escape(candidate.get("code") or ""),
             html.escape(candidate.get("name") or ""),
             html.escape(candidate.get("size") or ""),
             html.escape(candidate.get("colour") or ""), price))
        rows[-1] = rows[-1][:-5] + (
            '<br><a href="%s">Choose %s</a></td>' %
            (link_html, html.escape(candidate.get("code") or "")))
    link_html = html.escape(link, quote=True)
    amount = money(order.get("total_minor") or 0)
    heading = ("Prices are shown on each frame:" if any_payment
               else "There is no extra charge for any of them:")
    body = """<p>Hi {name},</p>
<p>Thank you for your order <strong>OW-{order}</strong>. Before dispatch we check every frame, and the <strong>{failed_name} ({failed_code})</strong> frame you chose did not pass our quality check, so we won't send it to you.</p>
<p>Your original choice: {failed_code} {failed_name}, {colour}, size {size}<br>Did not pass quality check</p>
<p>We have similar frames in stock that suit your progressive lenses. {heading}</p>
<table role="presentation"><tr>{candidates}</tr></table>
<p><a href="{link}">Choose a replacement frame</a></p>
<p>If you'd rather not change your frame, you can cancel and get a full refund of {amount} to your original payment method.</p>
<p><a href="{link}">Cancel and refund me in full</a></p>
<p>Please choose within 7 days. Once you choose, we fit your lenses and dispatch. Sorry for the delay.</p>
<p>— Optiwar Team</p>""".format(
        name=html.escape(first_name(order.get("firstname"))),
        order=html.escape(str(order.get("order_id") or "")),
        failed_name=html.escape(failed.get("name") or ""),
        failed_code=html.escape(failed.get("code") or ""),
        colour=html.escape(failed.get("colour") or ""),
        size=html.escape(failed.get("size") or ""),
        heading=heading, candidates="".join(rows), link=link_html, amount=amount)
    text_body = re.sub(r'<a href="([^"]+)">([^<]+)</a>', r"\2 (\1)", body)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text_body))
    text = re.sub(r"\s+", " ", text).strip()
    return body, text


def wa_values(order, failed, link):
    return [first_name(order.get("firstname")),
            "OW-%s" % order.get("order_id"),
            "%s (%s)" % (failed.get("name") or "", failed.get("code") or ""),
            link]


def diff_minor(candidate, failed):
    return int(candidate.get("price_minor") or 0) - int(failed.get("price_minor") or 0)


def is_inr_currency(currency):
    return str(currency or "").strip().upper() == "INR"


def upi_fallback_state(error_text, payment_ref, call):
    if ("not enough balance" not in str(error_text or "").lower()
            or not payment_ref):
        return None
    ok, response = call("/payments/%s/refunds" % quote(str(payment_ref), safe=""),
                        method="GET")
    if not ok or not isinstance(response, dict):
        return {"count": None, "enabled": False}
    count = len(response.get("items") or [])
    return {"count": count, "enabled": count == 0}
