"""Dry-run-first day-three reminders and day-seven expiry for frame offers."""

import argparse
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta

import config
from services import frame_offers, notifications
from services import optiwar_orders as ow_orders


def _date(value):
    if isinstance(value, datetime):
        return value
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return parsedate_to_datetime(text).replace(tzinfo=None)


def run(apply=False, now=None, client=None, notify=None, orders=None):
    client = client or frame_offers
    notify = notify or notifications
    orders = orders or ow_orders
    now = now or datetime.now()
    ok, result = client.open_offers()
    if not ok:
        return [{"action": "error", "error": str(result)}]
    actions = []
    for offer in result.get("offers") or []:
        if offer.get("reason") != "QC_FAILED":
            continue
        created = _date(offer["created_at"])
        expires = _date(offer["expires_at"])
        offer_uuid = offer["offer_uuid"]
        if now >= expires:
            actions.append({"action": "expire", "offer_uuid": offer_uuid})
            if apply:
                email_ok, _detail = notify.send_email(
                    config.OPTIWAR_ADMIN_CC or "admin@optiwar.com",
                    "OW-%s: no reply to frame replacement offer in 7 days"
                    % offer["order_id"],
                    "<p>There was no reply to frame offer %s for order OW-%s in 7 days.</p>"
                    % (offer_uuid, offer["order_id"]))
                if email_ok:
                    client.mark(offer_uuid, "alerted")
                    client.mark(offer_uuid, "expired")
            continue
        if (offer.get("reminder_sent_at") is not None
                or now - created < timedelta(days=3)):
            continue
        template = frame_offers.wa_template()
        if not template:
            actions.append({"action": "waiting_for_template", "offer_uuid": offer_uuid})
            continue
        actions.append({"action": "remind", "offer_uuid": offer_uuid})
        if not apply:
            continue
        order = orders.get_order(offer["order_id"])
        if not order:
            continue
        link_ok, link_result = client.reminder_link(offer_uuid)
        if not link_ok:
            continue
        product = next((p for p in order.get("products", [])
                        if int(p.get("order_product_id") or 0)
                        == int(offer["order_line_id"])), {})
        failed = {"name": product.get("name") or "",
                  "code": product.get("model") or "",
                  "size": product.get("size") or "",
                  "colour": product.get("product_color") or "",
                  "price_minor": int(offer.get("failed_price_minor") or 0)}
        order["order_quantity"] = int(
            offer.get("order_quantity") or product.get("quantity") or 1)
        order["total_minor"] = int(round(float(order.get("total") or 0) * 100))
        order["refund_balance_minor"] = offer.get("refund_balance_minor")
        email_html, email_text = frame_offers.customer_email(
            order, failed, offer.get("candidates") or [], link_result["link"])
        subject = "Reminder: " + frame_offers.email_subject(offer["order_id"])
        email_ok, email_detail = notify.send_email(
            order.get("email") or "", subject, email_html, email_text,
            from_addr=config.OPTIWAR_FROM_EMAIL,
            admin_cc=config.OPTIWAR_ADMIN_CC or None)
        wa_ok, wa_detail = notify.send_msg91_wa_template(
            order.get("telephone") or "", template,
            body_values=frame_offers.wa_values(order, failed, link_result["link"]),
            lang="en", country_code="IN")
        for channel, recipient, sent, detail in (
                ("email", order.get("email") or "", email_ok, email_detail),
                ("whatsapp", order.get("telephone") or "", wa_ok, wa_detail)):
            notify._save_notification_log(
                offer["order_id"], channel, "ow_frame_qc_offer_reminder",
                recipient, sent, str(detail or ""), "frame_offer_reminder",
                details='{"offer_uuid":"%s"}' % offer_uuid)
        if email_ok and wa_ok:
            client.mark(offer_uuid, "reminder_sent")
    return actions


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="send reminders and expire offers (default: dry run)")
    args = parser.parse_args(argv)
    actions = run(apply=args.apply)
    for action in actions:
        print(action)


if __name__ == "__main__":
    main()
