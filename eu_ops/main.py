"""LensBazaar Order Processing Platform — FastAPI Application."""
import json
import logging
import os
import sys
import re
import datetime as dt
import decimal
from datetime import datetime
from html import unescape
import subprocess
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

from fastapi import FastAPI, Request, Form, Depends, HTTPException, Query, UploadFile, File
from typing import List
from fastapi.responses import (HTMLResponse, RedirectResponse, JSONResponse, FileResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
import auth
from database import init_platform_tables, db_cursor, ow_cursor, ow_ops_cursor, ow_write_enabled
from services import orders, approvals
from services import fedex
from services import notifications
from services import optiwar_orders as ow_orders
from services import merchant as merchant_svc
from services import einvoice
from services import order_edits
from services import support_email
from services import reports as reports_svc
from services import fda as fda_svc
from services import delhivery as delhivery_svc
from services import dtdc as dtdc_svc
from services import manual_shipments as manual_ship_svc
from services import reship as reship_svc
from services import face_scan
from services import rx_documents
from services import ow_rx
from services import reverse_pickup as rp_svc
from services import rp_lifecycle as rpl
from services import recon as recon_svc
from services import ow_new_order as ow_new
from services import payments_razorpay as rzp
from services import optiwar_refunds as ow_refunds
from services import invoice_dates
from services import invoice_series
from services import frame_offers as frame_offers_svc

ROOT_PATH = os.getenv("OPP_ROOT_PATH", "")
app = FastAPI(title=config.APP_TITLE, docs_url="/api/docs", redoc_url=None)

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["track_url"] = config.courier_track_url
templates.env.globals["fmt_mm"] = face_scan.fmt_mm
templates.env.globals["fmt_measured_at"] = face_scan.fmt_measured_at
templates.env.globals["rx_eye_display"] = ow_rx.format_eye
templates.env.globals["rx_eye_parse"] = ow_rx.parse_eye
templates.env.globals["rx_eye_form"] = ow_rx.format_eye_input
templates.env.globals["rx_needs_add"] = ow_rx.needs_add
templates.env.globals.update({
    "rx_doc_id": rx_documents.document_id,
    "rx_doc_comparison": rx_documents.comparison,
    "rx_doc_is_purged": rx_documents.is_purged,
    "rx_doc_typed_rows": rx_documents.typed_rows,
    "rx_doc_parsed": rx_documents.parsed,
    "rx_doc_parsed_rows": rx_documents.parsed_rows,
    "rx_doc_review_label": rx_documents.review_label,
    "rx_doc_source_label": rx_documents.source_label,
    "rx_doc_photo_pages": rx_documents.photo_pages,
})


# ─── Startup ───
@app.on_event("startup")
def startup():
    init_platform_tables()
    manual_ship_svc.init_tables()
    reship_svc.init_tables()
    rp_svc.init_tables()
    rpl.init_tables()
    # Create default admin if no users exist
    user = auth.get_user_by_username("admin")
    bootstrap_password = os.getenv("OPS_ADMIN_BOOTSTRAP_PASSWORD", "")
    if not user and bootstrap_password:
        auth.create_user("admin", bootstrap_password, "lensebazaar@gmail.com", "admin", "Admin User")
        print("[INIT] Default admin user created from OPS_ADMIN_BOOTSTRAP_PASSWORD")


# ─── Auth dependency ───
def get_current_user(request: Request) -> dict:
    token = request.cookies.get("session_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = auth.decode_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = auth.get_user_by_id(int(payload["sub"]))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_admin(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def optional_user(request: Request) -> dict | None:
    try:
        return get_current_user(request)
    except HTTPException:
        return None


# ─── Template context helper ───
def ctx(request: Request, user: dict = None, **kwargs):
    reship_ready = 0
    rp_open = 0
    if user and reship_svc.configured():
        try:
            reship_ready = reship_svc.ready_to_ship_count()
        except Exception:
            reship_ready = 0
    if user:
        try:
            rp_open = rpl.actionable_count()
        except Exception as e:
            logging.warning("reverse-pickup count failed: %s", e)
    return {"request": request, "user": user, "statuses": config.ORDER_STATUSES, "base": ROOT_PATH,
            "reship_ready": reship_ready, "rp_open": rp_open, "rp_live": rpl.live_steps(),
            **kwargs}


def render(name: str, request: Request, user: dict = None, **kwargs):
    context = ctx(request, user, **kwargs)
    return templates.TemplateResponse(request=request, name=name, context=context)


def _ow_rx_editable(order: dict) -> bool:
    locked_statuses = {
        "shipped", "delivered", "cancelled", "canceled", "refunded",
        "complete", "completed", "returned",
    }
    if str(order.get("status_name") or "").strip().lower() in locked_statuses:
        return False
    return not any(
        str(awb.get("awb_status") or "").strip().lower() not in {"cancelled", "canceled"}
        for awb in (order.get("awbs") or [])
        if isinstance(awb, dict)
    )


def _load_ow_awbs(order_id: str):
    with ow_cursor() as cur:
        cur.execute("""SELECT * FROM ops_shipping_awb WHERE ow_order_id = %s
                       ORDER BY id DESC""", (order_id,))
        return cur.fetchall() or []


def _format_rx_change_time(value) -> str:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.strftime("%d-%m-%Y %H:%M")
    if value:
        try:
            return dt.datetime.fromisoformat(str(value)).strftime("%d-%m-%Y %H:%M")
        except (TypeError, ValueError):
            return str(value)
    return ""


# ═══════════════════════════════════════════
# AUTH ROUTES
# ═══════════════════════════════════════════
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = optional_user(request)
    if user:
        return RedirectResponse(f"{ROOT_PATH}/dashboard", status_code=302)
    return render("pages/login.html", request)


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = auth.get_user_by_username(username)
    if not user or not auth.verify_password(password, user["password_hash"]):
        return render("pages/login.html", request, error="Invalid username or password")

    # Generate OTP and send via email
    otp = auth.generate_otp()
    auth.store_otp(user["id"], otp)

    # Try to send OTP email
    ok, msg = notifications.send_otp_email(user["email"], otp)
    if not ok:
        # If SMTP not configured, skip OTP for now (dev mode)
        token = auth.create_token(user["id"], user["username"], user["role"])
        auth.log_activity(user["id"], "login", ip=request.client.host)
        response = RedirectResponse(f"{ROOT_PATH}/dashboard", status_code=302)
        response.set_cookie("session_token", token, httponly=True, max_age=config.JWT_EXPIRE_MINUTES * 60)
        return response

    return render("pages/otp_verify.html", request, user_id=user["id"], email=user["email"],
            message=f"Verification code sent to {user['email']}")


@app.post("/verify-otp")
async def verify_otp_submit(request: Request, user_id: int = Form(...), otp_code: str = Form(...)):
    if auth.verify_otp(user_id, otp_code):
        user = auth.get_user_by_id(user_id)
        token = auth.create_token(user["id"], user["username"], user["role"])
        auth.log_activity(user["id"], "login_otp", ip=request.client.host)
        response = RedirectResponse(f"{ROOT_PATH}/dashboard", status_code=302)
        response.set_cookie("session_token", token, httponly=True, max_age=config.JWT_EXPIRE_MINUTES * 60)
        return response
    return render("pages/otp_verify.html", request, user_id=user_id, error="Invalid or expired code")


@app.get("/logout")
async def logout():
    response = RedirectResponse(f"{ROOT_PATH}/login", status_code=302)
    response.delete_cookie("session_token")
    return response


# ═══════════════════════════════════════════
# DASHBOARD
# ═══════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(f"{ROOT_PATH}/dashboard", status_code=302)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(get_current_user)):
    status_counts = orders.get_order_counts_by_status()
    pending_approvals = approvals.list_pending_approvals() if user["role"] == "admin" else []
    recent_eu = orders.list_orders(page=1, per_page=10)
    # Optiwar orders
    try:
        ow_status_counts = ow_orders.get_order_counts_by_status()
        recent_ow = ow_orders.list_orders(page=1, per_page=10)
    except Exception:
        ow_status_counts = []
        recent_ow = {"orders": []}
    return render("pages/dashboard.html", request, user, status_counts=status_counts,
            ow_status_counts=ow_status_counts,
            pending_approvals=pending_approvals,
            recent_orders=recent_eu["orders"],
            recent_ow_orders=recent_ow["orders"])


# ═══════════════════════════════════════════
# NOTES / PLATES
# ═══════════════════════════════════════════
@app.get("/notes", response_class=HTMLResponse)
async def notes_page(request: Request, user: dict = Depends(get_current_user),
                     page: int = 1, supplier: str = None, status: str = "active",
                     search: str = None):
    result = orders.list_notes(page=page, supplier=supplier, status=status, search=search)
    supplier_list = orders.get_supplier_list()
    return render("pages/notes.html", request, user, **result,
                  suppliers=supplier_list, current_supplier=supplier,
                  current_status=status, current_search=search)


@app.post("/notes/{supplier_id}/toggle")
async def toggle_note(request: Request, supplier_id: int, is_done: int = Form(...),
                      user: dict = Depends(get_current_user)):
    ok = orders.toggle_note_done(supplier_id, is_done)
    auth.log_activity(user["id"], f"note_{'done' if is_done else 'undone'}",
                      "supplier_order", supplier_id, ip=request.client.host)
    return JSONResponse({"ok": ok})


# ═══════════════════════════════════════════
# PROCESSING (unified view of all sites)
# ═══════════════════════════════════════════
@app.get("/processing", response_class=HTMLResponse)
async def processing_page(request: Request, user: dict = Depends(get_current_user),
                          page: int = 1, search: str = None,
                          source: str = None, status: str = None):
    result = orders.list_processing_orders(
        page=page, search=search, source_filter=source, status_filter=status)
    return render("pages/processing.html", request, user, **result,
                  current_search=search, current_source=source or "all",
                  current_status=status or "")


# ═══════════════════════════════════════════
# ORDERS
# ═══════════════════════════════════════════
@app.get("/orders", response_class=HTMLResponse)
async def orders_list(request: Request, user: dict = Depends(get_current_user),
                      page: int = 1, status: str = None, search: str = None,
                      source: str = None):
    current_source = source or "eu"
    if current_source == "optiwar":
        show_archived = request.query_params.get("archived") == "1"
        result = ow_orders.list_orders(page=page, status=status if status and status.strip() else None,
                                       search=search, show_archived=show_archived)
        all_statuses = ow_orders.get_status_list()
        return render("pages/orders.html", request, user, **result, all_statuses=all_statuses,
                current_status=status, current_search=search, current_source=current_source,
                show_archived=show_archived)
    else:
        status_id = int(status) if status and status.strip() else None
        result = orders.list_orders(page=page, status_id=status_id, search=search)
        all_statuses = orders.get_order_statuses()
        return render("pages/orders.html", request, user, **result, all_statuses=all_statuses,
                current_status=status_id, current_search=search, current_source=current_source)


@app.post("/orders/ow/archive")
async def ow_archive_orders(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    order_ids = form.getlist("order_ids")
    if not order_ids:
        single = form.get("order_id")
        if single:
            order_ids = [single]
    count = 0
    for oid in order_ids:
        if ow_orders.archive_order(oid):
            count += 1
    auth.log_activity(user["id"], f"archived {count} optiwar order(s)", "optiwar_order", 0, request.client.host)
    return RedirectResponse(f"{ROOT_PATH}/orders?source=optiwar", status_code=302)


@app.post("/orders/ow/{order_id}/remove")
async def ow_remove_order(request: Request, order_id: str,
                          action: str = Form(...), reason: str = Form(""),
                          user: dict = Depends(require_admin)):
    """Archive (reversible) or hard-delete (snapshotted) an optiwar order."""
    back = request.headers.get("referer") or f"{ROOT_PATH}/processing"
    reason = (reason or "").strip()
    if action == "archive":
        ok, err = ow_orders.archive_order_audited(order_id, reason, user["id"])
        verb = "archived"
    elif action == "delete":
        ok, err = ow_orders.delete_order(order_id, reason, user["id"])
        verb = "deleted"
    else:
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}err=bad_action",
                                status_code=302)
    if not ok:
        return RedirectResponse(f"{back}{'&' if '?' in back else '?'}err={quote(err or 'failed')}",
                                status_code=302)
    auth.log_activity(user["id"], f"{verb} optiwar order {order_id}" + (f": {reason}" if reason else ""),
                      "optiwar_order", 0, request.client.host)
    return RedirectResponse(f"{back}{'&' if '?' in back else '?'}msg={verb}+{order_id}",
                            status_code=302)


@app.post("/orders/ow/unarchive")
async def ow_unarchive_orders(request: Request, user: dict = Depends(get_current_user)):
    form = await request.form()
    order_ids = form.getlist("order_ids")
    if not order_ids:
        single = form.get("order_id")
        if single:
            order_ids = [single]
    count = 0
    for oid in order_ids:
        if ow_orders.unarchive_order(oid):
            count += 1
    auth.log_activity(user["id"], f"unarchived {count} optiwar order(s)", "optiwar_order", 0, request.client.host)
    return RedirectResponse(f"{ROOT_PATH}/orders?source=optiwar&archived=1", status_code=302)


def _fmt_rx_eye(raw):
    """Parse an Optiwar rx eye string 'sph/cyl/axis/add' into aligned SPH/CYL/AXIS/ADD."""
    parts = [p.strip() for p in str(raw or "").split("/")]
    while len(parts) < 4:
        parts.append("")

    def num(v, axis=False):
        v = (v or "").strip()
        if v == "":
            return "—"
        try:
            f = float(v)
        except ValueError:
            return v
        if axis:
            return str(int(round(f)))
        return f"{f:+.2f}"
    sph, cyl, axis, add = parts[0], parts[1], parts[2], parts[3]
    return (f"SPH {num(sph):>7}  CYL {num(cyl):>7}  "
            f"AXIS {num(axis, axis=True):>4}  ADD {num(add):>6}")


def _build_ow_copy_text(order: dict) -> str:
    """Build a clean WhatsApp-ready text block for vendors from an Optiwar order."""
    lines = [f"Optiwar Order OW-{order.get('order_id')}", ""]
    for p in order.get("products", []):
        code = (p.get("model") or "").strip()
        name = (p.get("name") or "").strip()
        head = f"Product: {name}"
        if code:
            head += f"  ({code})"
        lines.append(head)
        if p.get("product_color"):
            lines.append(f"Color: {p['product_color']}")
        if p.get("product_type"):
            lines.append(f"Type: {p['product_type']}")
        if p.get("box_number"):
            lines.append(f"Box no.: {p['box_number']}")
        lines.append(f"Qty: {p.get('quantity', 1)}")

        rx = p.get("rx") or {}
        if rx.get("recommendations"):
            lines.append(f"Lens type: {rx['recommendations']}")
        if rx.get("right_eye") or rx.get("left_eye"):
            lines.append("")
            lines.append("Prescription (SPH / CYL / AXIS / ADD)")
            if rx.get("right_eye"):
                lines.append(f"R:  {_fmt_rx_eye(rx['right_eye'])}")
            if rx.get("left_eye"):
                lines.append(f"L:  {_fmt_rx_eye(rx['left_eye'])}")
        addons = []
        for i in (1, 2, 3):
            nm = (rx.get(f"addon_{i}_name") or "").strip()
            if nm:
                addons.append(nm)
        if addons:
            lines.append("")
            lines.append("Add-ons: " + ", ".join(addons))
        lines.append("")
    return "\n".join(lines).strip()


@app.get("/orders/ow/{order_id}", response_class=HTMLResponse)
async def ow_order_detail(request: Request, order_id: str, user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    order["awbs"] = _load_ow_awbs(order_id)
    rx_ids = list(dict.fromkeys(
        p.get("rx_id") for p in order.get("products", [])
        if p.get("rx_id") is not None
    ))
    rx_last_change = ow_orders.last_rx_change(rx_ids)
    for change in rx_last_change.values():
        changed_by = change.get("changed_by")
        try:
            staff = auth.get_user_by_id(changed_by) if changed_by is not None else None
        except Exception as e:
            logging.warning("Could not resolve RX change staff %s: %s", changed_by, e)
            staff = None
        change["changed_by_name"] = (
            (staff or {}).get("full_name") or (staff or {}).get("username")
            or str(changed_by or "Ops")
        )
        change["changed_at_display"] = _format_rx_change_time(change.get("changed_at"))
    notif_logs = notifications.get_notification_logs(order_id)
    # Payment links live in the ops database, so an order created here can show
    # whether a link was sent without leaving the order page.
    pay_links = rzp.links_for_order(order_id)
    with ow_cursor() as cur:
        cur.execute("SELECT id FROM ops_order_drafts WHERE order_id = %s "
                    "ORDER BY id DESC LIMIT 1", (order_id,))
        row = cur.fetchone()
    # Reship pilot: Optiwar's queue API is the only source of truth for PAID.
    reship = None
    if order["awbs"] and reship_svc.configured():
        reship = reship_svc.status_for(order_id, user["id"], context="page")
        reship["booked"] = reship_svc.booked_for(order_id, reship) if reship["paid"] else None
        reship["syncs"] = reship_svc.syncs_for(order_id)
        reship["pending_syncs"] = [s for s in reship["syncs"] if s["state"] != "synced"]
        reship["received_sync"] = reship_svc.has_return_received_sync(order_id)
    rpickups = rp_svc.list_for(order_id)
    rp_syncs = [x for x in reship_svc.syncs_for(order_id)
                if x["kind"] in (rp_svc.SYNC_KIND, rp_svc.SYNC_CANCEL_KIND)]
    rp_fee_error = rpl.refresh_state(order_id) if rpickups else ""
    for p in rpickups:
        p["syncs"] = [x for x in rp_syncs if x["local_ref_id"] == p["id"]]
        p["notif"] = rp_svc.notification_view(p["syncs"])
        if p["status"] != "cancelled":
            p["case"] = rpl.case_view(p, track=True)
            p["timeline"] = rpl.timeline(order_id, p, p["case"]["life"], p["case"]["fee"])
    face = face_scan.fetch(order_id) if face_scan.is_live() else None
    if face:
        for line in face.get("lines") or []:
            scan = line.get("scan") if isinstance(line, dict) else None
            if isinstance(scan, dict) and isinstance(scan.get("capture"), dict):
                scan["capture"]["scan_id"] = face_scan.scan_id_from_url(
                    scan["capture"].get("url"))
    frame_offer_by_line = {}
    for product in order.get("products") or []:
        line_id = product.get("order_product_id")
        if line_id is not None:
            ok, offer = frame_offers_svc.latest(order_id, line_id)
            frame_offer_by_line[str(line_id)] = offer if ok else None
    frame_offer_currency = ""
    if any(offer and not offer.get("settled_at")
           and offer.get("settlement") in ("REFUND_DUE", "PAYMENT_DUE")
           for offer in frame_offer_by_line.values()):
        try:
            frame_offer_currency = str(
                _refund_facts(order_id).get("currency") or "").upper()
        except Exception as exc:
            logging.warning("Frame-offer currency unavailable for %s: %s",
                            order_id, exc)
    return render("pages/ow_order_detail.html", request, user, order=order,
                  notif_logs=notif_logs, ow_statuses=ow_orders.get_status_list(),
                  accounts_email_default=config.OPTIWAR_ACCOUNTS_EMAIL,
                  pay_links=pay_links, pay_draft_id=(row or {}).get("id"),
                  copy_text=_build_ow_copy_text(order),
                  reship=reship, reships=reship_svc.list_reships(order_id),
                  receipts=reship_svc.list_receipts(order_id),
                  receipt_conditions=reship_svc.CONDITIONS, rpickups=rpickups,
                  rp_fee=rpl.fee_view(order_id), rp_fee_error=rp_fee_error,
                  rp_waivers=rpl.waivers_for(order_id), rp_waiver_codes=rpl.WAIVER_CODES,
                  rp_conditions=rpl.CONDITIONS, rp_reasons=rp_svc.REASONS,
                  face_scan=face,
                  frame_offer_by_line=frame_offer_by_line,
                  frame_offer_currency=frame_offer_currency,
                  frame_offer_inr=frame_offers_svc.is_inr_currency(
                      frame_offer_currency),
                  rx_docs_live=rx_documents.is_live(),
                  rx_last_change=rx_last_change,
                  rx_editable=_ow_rx_editable(order),
                  rp_ship_types=rpl.SHIPMENT_TYPES)


def _frame_offer_context(order, product, result=None, error=""):
    matches = (result or {}).get("matches") or []
    chosen = {int(row["product_id"]) for row in matches[:4]}
    failed = dict((result or {}).get("failed_product") or {})
    failed["price_minor"] = int((result or {}).get("failed_price_minor") or 0)
    failed.setdefault("size", "")
    failed.setdefault("colour", "")
    order_copy = dict(order)
    order_copy["total_minor"] = int(round(float(order.get("total") or 0) * 100))
    candidates = [row for row in matches if int(row["product_id"]) in chosen]
    email_html, email_text = frame_offers_svc.customer_email(
        order_copy, failed, candidates, "https://%s/fq/<one-time-link>" %
        ("in.optiwar.com" if order.get("site_from") == "in.optiwar.com"
         else "optiwar.com"))
    return {"order": order, "product": product, "matches": matches,
            "selected_ids": chosen, "failed": failed, "reason": "QC_FAILED",
            "wa_template": frame_offers_svc.wa_template(),
            "email_html": email_html, "email_text": email_text,
            "wa_body": frame_offers_svc.WA_BODY, "error": error}


@app.get("/orders/ow/{order_id}/frame-offer", response_class=HTMLResponse)
async def ow_frame_offer_form(request: Request, order_id: str,
                              order_line_id: int = Query(...),
                              error: str = Query(default=""),
                              user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    product = next((p for p in order.get("products", [])
                    if int(p.get("order_product_id") or 0) == order_line_id), None)
    if not product:
        raise HTTPException(status_code=404, detail="Order line not found")
    ok, result = frame_offers_svc.matches(order_id, order_line_id)
    if not ok:
        error = error or str(result)
        result = {}
    context = _frame_offer_context(order, product, result, error)
    return render("pages/ow_frame_offer.html", request, user, **context)


@app.post("/orders/ow/{order_id}/frame-offer")
async def ow_frame_offer_send(request: Request, order_id: str,
                              order_line_id: int = Form(...),
                              reason: str = Form("QC_FAILED"),
                              product_ids: List[str] = Form([]),
                              user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    product = next((p for p in order.get("products", [])
                    if int(p.get("order_product_id") or 0) == order_line_id), None)
    if not product:
        raise HTTPException(status_code=404, detail="Order line not found")
    reason = (reason or "").upper()
    wa_template = frame_offers_svc.wa_template()
    allowed, gate_message = frame_offers_svc.send_gate(reason, wa_template)
    if not allowed:
        return RedirectResponse(
            "%s/orders/ow/%s/frame-offer?order_line_id=%s&error=%s" %
            (ROOT_PATH, quote(order_id), order_line_id,
             quote(gate_message)),
            status_code=303)
    ok, match_result = frame_offers_svc.matches(order_id, order_line_id)
    if not ok:
        raise HTTPException(status_code=502, detail=str(match_result))
    selected = set(int(pid) for pid in product_ids)
    candidates = [c for c in match_result.get("matches", [])
                  if int(c["product_id"]) in selected]
    if len(candidates) != len(selected) or not 1 <= len(selected) <= 6:
        raise HTTPException(status_code=400, detail="Choose one to six current matches")
    created_by = (user.get("full_name") or user.get("username") or "Ops")
    ok, created = frame_offers_svc.create(
        order_id, order_line_id, reason, list(selected), created_by)
    if not ok:
        raise HTTPException(status_code=502, detail=str(created))
    failed = dict(match_result.get("failed_product") or {})
    failed["price_minor"] = int(match_result.get("failed_price_minor") or 0)
    failed.setdefault("size", "")
    failed.setdefault("colour", "")
    order_copy = dict(order)
    order_copy["total_minor"] = int(round(float(order.get("total") or 0) * 100))
    email_html, email_text = frame_offers_svc.customer_email(
        order_copy, failed, created.get("candidates") or candidates,
        created["link"])
    subject = frame_offers_svc.email_subject(order_id)
    email_ok, email_detail = notifications.send_email(
        order.get("email") or "", subject, email_html, email_text,
        from_addr=config.OPTIWAR_FROM_EMAIL,
        admin_cc=config.OPTIWAR_ADMIN_CC or None)
    wa_ok, wa_detail = notifications.send_msg91_wa_template(
        order.get("telephone") or "", wa_template,
        body_values=frame_offers_svc.wa_values(order, failed, created["link"]),
        lang="en", country_code="IN")
    for channel, recipient, sent, detail in (
            ("email", order.get("email") or "", email_ok, email_detail),
            ("whatsapp", order.get("telephone") or "", wa_ok, wa_detail)):
        notifications._save_notification_log(
            order_id, channel, frame_offers_svc.EVENT, recipient, sent,
            str(detail or ""), created_by,
            details=json.dumps({"offer_uuid": created["offer_uuid"]}))
    summary = ("Frame replacement offer created; email %s; WhatsApp %s" %
               ("sent" if email_ok else "failed",
                "sent" if wa_ok else "failed"))
    return RedirectResponse(
        "%s/orders/ow/%s?success=%s" %
        (ROOT_PATH, quote(order_id), quote(summary)), status_code=303)


@app.post("/orders/ow/{order_id}/frame-offer/{offer_uuid}/settled")
async def ow_frame_offer_settled(order_id: str, offer_uuid: str,
                                 ref: str = Form(...),
                                 user: dict = Depends(get_current_user)):
    ok, result = frame_offers_svc.settled(
        order_id, offer_uuid, ref, user.get("full_name") or user.get("username") or "Ops")
    if not ok:
        raise HTTPException(status_code=502, detail=str(result))
    return RedirectResponse("%s/orders/ow/%s?success=Settlement recorded" %
                            (ROOT_PATH, quote(order_id)), status_code=303)


@app.post("/orders/ow/{order_id}/frame-offer/{offer_uuid}/payment-link")
async def ow_frame_offer_payment_link(order_id: str, offer_uuid: str,
                                      order_line_id: int = Form(...),
                                      user: dict = Depends(get_current_user)):
    ok, latest = frame_offers_svc.latest(order_id, order_line_id)
    if not ok:
        raise HTTPException(status_code=502, detail=str(latest))
    offer = latest or {}
    if offer.get("offer_uuid") != offer_uuid:
        raise HTTPException(status_code=404, detail="Frame offer not found")
    currency = str(_refund_facts(order_id).get("currency") or "").upper()
    if not frame_offers_svc.is_inr_currency(currency):
        raise HTTPException(
            status_code=400,
            detail="Frame price settlement requires INR; order currency is %s" %
            (currency or "unknown"))
    amount = int(offer.get("price_diff_minor") or 0)
    if amount <= 0 or amount % 100:
        raise HTTPException(status_code=400,
                            detail="Frame replacement payment links require a whole-rupee amount")
    order = ow_orders.get_order(order_id) or {}
    site = ("optiwar.in" if order.get("site_from") == "in.optiwar.com"
            else "optiwar.com")
    created, result = rzp.create_link(
        order_id, amount / 100, "INR",
        "in.optiwar.com" if order.get("site_from") == "in.optiwar.com" else "optiwar.com",
        name=order.get("firstname") or "", email=order.get("email") or "",
        phone=order.get("telephone") or "",
        description="Frame replacement price difference",
        created_by=user.get("username") or "ops")
    if not created:
        raise HTTPException(status_code=502, detail=str(result))
    if rzp.claim_link_notify(result.get("link_id", "")):
        display = frame_offers_svc.money(amount)
        order.update({
            "items": [{"name": "Frame replacement price difference", "qty": 1,
                       "total": display}],
            "total_display": display,
            "pay_link": result.get("short_url", ""),
            "site": site,
        })
        sent = notifications.notify_ow_new_order(
            order, channels=["email", "whatsapp"], use_ai=False)
        if not _notify_ok(sent):
            rzp.release_link_notify(result.get("link_id", ""))
            raise HTTPException(status_code=502, detail="Payment link notification failed")
    return RedirectResponse(
        "%s/orders/ow/%s?success=%s" %
        (ROOT_PATH, quote(order_id), quote("Payment link created and sent to customer")),
        status_code=303)


@app.post("/orders/ow/{order_id}/rx-change")
async def ow_rx_change(request: Request, order_id: str,
                       rx_id: str = Form(""),
                       r_sph: str = Form(""), r_cyl: str = Form(""),
                       r_axis: str = Form(""), r_add: str = Form(""),
                       l_sph: str = Form(""), l_cyl: str = Form(""),
                       l_axis: str = Form(""), l_add: str = Form(""),
                       reference: str = Form(""), confirmed: str = Form(""),
                       notify_email: str = Form(None),
                       notify_whatsapp: str = Form(None),
                       notify_sms: str = Form(None),
                       dry_run: str = Form("0"),
                       user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order["awbs"] = _load_ow_awbs(order_id)
    if not _ow_rx_editable(order):
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{quote(order_id, safe='')}?error="
            f"{quote('Prescription cannot be changed after shipment or AWB issuance')}",
            status_code=302)

    rx_id = (rx_id or "").strip()
    product = next((
        p for p in order.get("products", [])
        if p.get("rx_id") is not None and str(p.get("rx_id")) == rx_id
    ), None)
    if not product:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{quote(order_id, safe='')}?error="
            f"{quote('Prescription ID is not attached to this order')}",
            status_code=302)

    reference = (reference or "").strip()
    if not reference:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{quote(order_id, safe='')}?error="
            f"{quote('A ticket or chat reference is required')}",
            status_code=302)
    if confirmed != "1":
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{quote(order_id, safe='')}?error="
            f"{quote('Customer confirmation is required')}",
            status_code=302)

    current_rx = product.get("rx") or {}
    require_add = ow_rx.needs_add(current_rx.get("recommendations"))
    new_right, right_errors = ow_rx.validate_eye(
        r_sph, r_cyl, r_axis, r_add, require_add)
    new_left, left_errors = ow_rx.validate_eye(
        l_sph, l_cyl, l_axis, l_add, require_add)
    errors = ([f"Right eye: {e}" for e in right_errors]
              + [f"Left eye: {e}" for e in left_errors])
    if errors:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{quote(order_id, safe='')}?error="
            f"{quote('; '.join(errors))}",
            status_code=302)

    old_right = str(current_rx.get("right_eye") or "")
    old_left = str(current_rx.get("left_eye") or "")
    old_right_values = ow_rx.parse_eye(old_right)
    old_left_values = ow_rx.parse_eye(old_left)
    if old_right_values == ow_rx.parse_eye(new_right) and old_left_values == ow_rx.parse_eye(new_left):
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{quote(order_id, safe='')}?error="
            f"{quote('The new prescription is identical to the current values')}",
            status_code=302)

    old_rx = {"right_eye": old_right, "left_eye": old_left}
    new_rx = {"right_eye": new_right, "left_eye": new_left}
    if dry_run == "1":
        _, email_html, _ = notifications.build_ow_rx_changed_email(order, old_rx, new_rx)
        return HTMLResponse(content=email_html)

    changed_by_name = (user.get("full_name") or user.get("username")
                       or str(user.get("id") or "Ops"))
    try:
        change = ow_orders.update_rx_powers(
            order, rx_id, new_right, new_left, reference,
            user.get("id"), changed_by_name)
    except ValueError as e:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{quote(order_id, safe='')}?error={quote(str(e))}",
            status_code=302)

    channels = []
    if notify_email:
        channels.append("email")
    if notify_whatsapp:
        channels.append("whatsapp")
    if notify_sms:
        channels.append("sms")
    try:
        results = notifications.notify_ow_rx_changed(
            order, change["old"], change["new"], channels) or {}
    except Exception as e:
        logging.warning("Optiwar prescription notification failed for %s: %s", order_id, e)
        results = {}

    labels = {"email": "Email", "whatsapp": "WhatsApp", "sms": "SMS"}
    for channel in channels:
        result = results.get(channel)
        sent = isinstance(result, (tuple, list)) and bool(result) and bool(result[0])
        provider_ref = ""
        if isinstance(result, (tuple, list)) and len(result) > 1:
            provider_ref = notifications._extract_reference(str(result[1]))
        note = (f"Prescription-change notification {'sent' if sent else 'failed'} "
                f"via {labels.get(channel, channel)}")
        if provider_ref:
            note += f" (ref {provider_ref})"
        ow_orders.add_history(order_id, note)

    auth.log_activity(
        user["id"], "ow_rx_changed", "optiwar_order", 0,
        {"order_id": order_id, "rx_id": rx_id,
         "old": change["old"], "new": change["new"],
         "reference": reference, "channels": channels},
        request.client.host if request.client else "")
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{quote(order_id, safe='')}?success="
        f"{quote('Prescription updated and customer notification processed')}",
        status_code=302)


@app.get("/orders/ow/{order_id}/face-scan/photo")
async def ow_face_scan_photo(request: Request, order_id: str, scan_id: str = "",
                             user: dict = Depends(get_current_user)):
    host = request.client.host if request.client else None

    def log_failure(reason):
        auth.log_activity(user["id"], "face_scan_photo_failed", "optiwar_order", 0,
                          {"order_id": order_id, "scan_id": scan_id or None,
                           "reason": reason}, host)

    if not face_scan.is_live():
        reason = "Face scan is not live"
        log_failure(reason)
        raise HTTPException(status_code=404, detail=reason)
    if scan_id and not scan_id.isdigit():
        reason = "scan_id must contain digits only"
        log_failure(reason)
        raise HTTPException(status_code=400, detail=reason)

    try:
        photo = face_scan.capture(order_id, int(scan_id) if scan_id else None)
    except face_scan.FaceScanError as exc:
        reason = str(exc)
        log_failure(reason)
        return Response(content=reason, status_code=502, media_type="text/plain")

    auth.log_activity(user["id"], "face_scan_photo_viewed", "optiwar_order", 0,
                      {"order_id": order_id, "scan_id": scan_id or None}, host)
    return Response(photo, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store, private",
                             "Content-Disposition": "inline"})


@app.get("/orders/ow/{order_id}/rx-documents/card", response_class=HTMLResponse)
async def ow_rx_documents_card(request: Request, order_id: str,
                               user: dict = Depends(get_current_user)):
    if not rx_documents.is_live():
        raise HTTPException(status_code=404, detail="Prescription documents are not live")
    documents = rx_documents.fetch(order_id, user)
    return render(
        "partials/ow_rx_documents.html", request, user,
        rx_docs=documents, order_id=order_id,
        order_reference=rx_documents.order_reference(order_id))


@app.get("/orders/ow/{order_id}/rx-documents/{document_id}/photo")
async def ow_rx_document_photo(request: Request, order_id: str, document_id: str,
                               page: str = "0",
                               user: dict = Depends(get_current_user)):
    host = request.client.host if request.client else None

    def log_failure(reason, page_value=None):
        auth.log_activity(user["id"], "rx_document_failed", "optiwar_order", 0,
                          {"order_id": order_id, "document_id": document_id,
                           "page": page if page_value is None else page_value,
                           "reason": reason}, host)

    if not rx_documents.is_live():
        reason = "Prescription documents are not live"
        log_failure(reason)
        raise HTTPException(status_code=404, detail=reason)
    if not re.fullmatch(r"[0-9]+", document_id):
        reason = "document_id must contain digits only"
        log_failure(reason)
        raise HTTPException(status_code=400, detail=reason)
    if not re.fullmatch(r"[0-9]+", page):
        reason = "page must contain digits only"
        log_failure(reason)
        raise HTTPException(status_code=400, detail=reason)

    page_number = int(page)
    try:
        body, content_type = rx_documents.photo(order_id, document_id, page_number, user)
    except rx_documents.RxDocumentError as exc:
        reason = str(exc)
        log_failure(reason, page_number)
        return Response(content=reason, status_code=502, media_type="text/plain")

    auth.log_activity(user["id"], "rx_document_viewed", "optiwar_order", 0,
                      {"order_id": order_id, "document_id": document_id,
                       "page": page_number}, host)
    return Response(body, media_type=content_type,
                    headers={"Cache-Control": "no-store, private",
                             "Content-Disposition": "inline"})


@app.get("/orders/ow/{order_id}/reverse-pickup", response_class=HTMLResponse)
async def ow_reverse_pickup_form(request: Request, order_id: str,
                                 user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    fee_ok, fee = rpl.fee_gate(order_id)
    return render("pages/ow_reverse_pickup.html", request, user, order=order,
                  fee_ok=fee_ok, fee=fee, waiver_codes=rpl.WAIVER_CODES,
                  decisions=rpl.DECISIONS,
                  waivers=rpl.waivers_for(order_id), waiver_pending=rpl.pending_for(None, order_id),
                  serviceable=rp_svc.serviceable(order.get("shipping_postcode") or ""),
                  existing=rp_svc.active_for(order_id), reasons=rp_svc.REASONS,
                  reference=rp_svc.next_reference(order_id),
                  forward=_latest_awb(order_id, full=True),
                  warehouse=delhivery_svc._pickup_location())


@app.post("/orders/ow/{order_id}/reverse-pickup")
async def ow_reverse_pickup_book(request: Request, order_id: str,
                                 reason: str = Form(...), remarks: str = Form(""),
                                 weight_kg: float = Form(0.25),
                                 length_cm: int = Form(17), width_cm: int = Form(17),
                                 height_cm: int = Form(7),
                                 products_desc: str = Form(""),
                                 user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if reason not in rp_svc.REASONS:
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}/reverse-pickup?error="
                                + quote("Pick a return reason"), status_code=302)
    fee_ok, fee = rpl.fee_gate(order_id)
    if not fee_ok:
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}/reverse-pickup?error=" + quote(
            "Booking refused: %s%s" % (
                fee.get("block_reason") or "fee not PAID or WAIVED",
                (" (" + fee["error"] + ")") if fee["error"] else "")),
            status_code=302)
    fwd = _latest_awb(order_id, full=True) or {}
    params = {"reason": reason, "remarks": remarks.strip(), "weight_kg": weight_kg,
              "length_cm": length_cm, "width_cm": width_cm, "height_cm": height_cm,
              "total_amount": order.get("total_inr", 0) or 1,
              "products_desc": products_desc.strip() or None, "shipping_mode": "Surface"}
    try:
        row, sync = rp_svc.book(order, params, user, forward_awb=fwd.get("tracking_number", ""))
    except delhivery_svc.DelhiveryError as e:
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}/reverse-pickup?error="
                                + quote(str(e)), status_code=302)
    rpl.record_booked(row, fee["state"], user)
    auth.log_activity(user["id"], "reverse_pickup_booked", "optiwar_order", 0,
                      {"order": order_id, "waybill": row["waybill"], "reference": row["reference"],
                       "reason": reason, "optiwar_sync": (sync or {}).get("state")},
                      request.client.host)
    if sync and sync["state"] == "synced":
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?msg=" + quote(
            "Delhivery reverse pickup booked: %s. Optiwar synchronized - check the card for "
            "customer email/WhatsApp status." % row["waybill"]) + "#rpCard", status_code=302)
    return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(
        "Reverse pickup booked with Delhivery (%s) but Optiwar was not notified: %s. "
        "Use Retry Sync - do not book again." % (row["waybill"], (sync or {}).get("last_error")
                                                or "no response")) + "#rpCard", status_code=302)


@app.post("/orders/ow/{order_id}/reverse-pickup/{pickup_id}/cancel")
async def ow_reverse_pickup_cancel(request: Request, order_id: str, pickup_id: int,
                                   user: dict = Depends(get_current_user)):
    row = rp_svc.get(pickup_id)
    if not row or row["ow_order_id"] != order_id:
        raise HTTPException(status_code=404, detail="Reverse pickup not found")
    why = rpl.received_block(pickup_id)
    if why:
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(why) + "#rpCard",
                                status_code=302)
    try:
        row, sync = rp_svc.cancel(pickup_id, user)
    except delhivery_svc.DelhiveryError as e:
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(
            "Delhivery cancel failed: %s" % e) + "#rpCard", status_code=302)
    auth.log_activity(user["id"], "reverse_pickup_cancelled", "optiwar_order", 0,
                      {"order": order_id, "waybill": row["waybill"],
                       "optiwar_sync": (sync or {}).get("state")}, request.client.host)
    msg = "Reverse pickup %s cancelled with Delhivery." % row["waybill"]
    if sync and sync["state"] != "synced":
        msg += " Optiwar not yet told - use Retry Sync."
    return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?msg=" + quote(msg) + "#rpCard",
                            status_code=302)


def _rp_back(request, order_id):
    back = request.headers.get("referer") or f"{ROOT_PATH}/orders/ow/{order_id}"
    back = back.split("#")[0]
    for k in ("msg=", "error="):
        if k in back:
            back = back.split("?")[0]
    return back


def _rp_result(request, order_id, action, row, user, ok_msg):
    """Redirect after an rp_* action: Optiwar result decides msg vs error."""
    auth.log_activity(user["id"], "reverse_pickup_" + action, "optiwar_order", 0,
                      {"order": order_id, "optiwar_sync": (row or {}).get("state"),
                       "sync_id": (row or {}).get("id")}, request.client.host)
    back = _rp_back(request, order_id)
    sep = "&" if "?" in back else "?"
    if row and row["state"] == "synced":
        return RedirectResponse(back + sep + "msg=" + quote(ok_msg + " Optiwar synchronized.")
                                + "#rpCard", status_code=302)
    return RedirectResponse(back + sep + "error=" + quote(
        "%s Recorded in Ops, but Optiwar has not accepted it yet: %s. Use Retry Sync - do not "
        "repeat the action." % (ok_msg, (row or {}).get("last_error") or "no response"))
        + "#rpCard", status_code=302)


def _rp_pickup(order_id, pickup_id):
    row = rp_svc.get(pickup_id)
    if not row or row["ow_order_id"] != order_id:
        raise HTTPException(status_code=404, detail="Reverse pickup not found")
    return row


def _rp_refused(request, order_id, e):
    back = _rp_back(request, order_id)
    sep = "&" if "?" in back else "?"
    return RedirectResponse(back + sep + "error=" + quote(str(e)) + "#rpCard", status_code=302)


@app.post("/orders/ow/{order_id}/reverse-pickup-fee/waive")
async def ow_rp_waive(request: Request, order_id: str, reason_code: str = Form(""),
                      note: str = Form(""), pickup_id: int = Form(0),
                      user: dict = Depends(get_current_user)):
    if not ow_orders.get_order(order_id):
        raise HTTPException(status_code=404, detail="Order not found")
    if pickup_id:
        _rp_pickup(order_id, pickup_id)
    try:
        row = rpl.request_waiver(order_id, pickup_id or None, reason_code, note, user)
    except rpl.LifecycleError as e:
        return _rp_refused(request, order_id, e)
    return _rp_result(request, order_id, "fee_waived", row, user,
                      "₹250 fee waiver (%s) recorded." % reason_code)


@app.post("/orders/ow/{order_id}/reverse-pickup/{pickup_id}/reason")
async def ow_rp_reason(request: Request, order_id: str, pickup_id: int, reason: str = Form(""),
                       user: dict = Depends(get_current_user)):
    _rp_pickup(order_id, pickup_id)
    if reason not in rp_svc.REASONS:
        return _rp_refused(request, order_id, "Pick a return reason")
    try:
        row = rpl.correct_reason(pickup_id, reason, user)
    except rpl.LifecycleError as e:
        return _rp_refused(request, order_id, e)
    return _rp_result(request, order_id, "reason_corrected", row, user,
                      "Return reason changed to %s (Delhivery booking unchanged)." % reason)


@app.post("/orders/ow/{order_id}/reverse-pickup/{pickup_id}/received")
async def ow_rp_received(request: Request, order_id: str, pickup_id: int,
                         parcel_condition: str = Form(""), notes: str = Form(""),
                         user: dict = Depends(get_current_user)):
    _rp_pickup(order_id, pickup_id)
    try:
        row = rpl.mark_received(pickup_id, parcel_condition, notes, user)
    except rpl.LifecycleError as e:
        return _rp_refused(request, order_id, e)
    return _rp_result(request, order_id, "received", row, user,
                      "REVERSE PICKUP RECEIVED recorded - inspect the return next.")


@app.post("/orders/ow/{order_id}/reverse-pickup/{pickup_id}/inspection")
async def ow_rp_inspection(request: Request, order_id: str, pickup_id: int,
                           manufacturing_defect: str = Form(""), remarks: str = Form(""),
                           evidence: List[UploadFile] = File(default=[]),
                           user: dict = Depends(get_current_user)):
    _rp_pickup(order_id, pickup_id)
    files = []
    for f in evidence or []:
        if f.filename:
            files.append((f.filename, await f.read()))
    try:
        row = rpl.inspect(pickup_id, manufacturing_defect, remarks, files, user)
    except rpl.LifecycleError as e:
        return _rp_refused(request, order_id, e)
    return _rp_result(request, order_id, "inspection_completed", row, user,
                      "Inspection recorded: MANUFACTURING DEFECT %s." % manufacturing_defect)


@app.post("/orders/ow/{order_id}/reverse-pickup-request/decision")
async def ow_rp_decision(request: Request, order_id: str, decision: str = Form(""),
                         note: str = Form(""), user: dict = Depends(get_current_user)):
    if not ow_orders.get_order(order_id):
        raise HTTPException(status_code=404, detail="Order not found")
    back = f"{ROOT_PATH}/orders/ow/{order_id}/reverse-pickup"
    try:
        row = rpl.decide_request(order_id, decision, note, user)
    except rpl.LifecycleError as e:
        return RedirectResponse(back + "?error=" + quote(str(e)), status_code=302)
    auth.log_activity(user["id"], "reverse_pickup_request_decided", "optiwar_order", 0,
                      {"order": order_id, "decision": decision, "sync_id": row["id"]},
                      request.client.host)
    return RedirectResponse(back + "?msg=" + quote(
        "Decision %s recorded at Optiwar; Optiwar emails the customer." % decision), status_code=302)


@app.post("/orders/ow/{order_id}/reverse-pickup/{pickup_id}/dispatched")
async def ow_rp_dispatched(request: Request, order_id: str, pickup_id: int,
                           shipment_type: str = Form(""),
                           user: dict = Depends(get_current_user)):
    _rp_pickup(order_id, pickup_id)
    try:
        row = rpl.confirm_dispatch(pickup_id, shipment_type, user)
    except rpl.LifecycleError as e:
        return _rp_refused(request, order_id, e)
    return _rp_result(request, order_id, "forward_dispatched", row, user,
                      "Forward shipment handed to the courier.")


@app.post("/orders/ow/{order_id}/reverse-pickup/{pickup_id}/complete")
async def ow_rp_complete(request: Request, order_id: str, pickup_id: int,
                         outcome: str = Form(""), note: str = Form(""),
                         user: dict = Depends(get_current_user)):
    _rp_pickup(order_id, pickup_id)
    try:
        row = rpl.complete(pickup_id, outcome, note, user)
    except rpl.LifecycleError as e:
        return _rp_refused(request, order_id, e)
    return _rp_result(request, order_id, "completed", row, user, "Return case COMPLETE.")


@app.get("/orders/ow/{order_id}/reverse-pickup-evidence/{evidence_id}")
async def ow_rp_evidence(order_id: str, evidence_id: int, user: dict = Depends(get_current_user)):
    ev = rpl.get_evidence(evidence_id)
    if not ev or ev["ow_order_id"] != order_id or not os.path.exists(ev["path"]):
        raise HTTPException(status_code=404, detail="Evidence not found")
    return FileResponse(ev["path"], filename=ev["filename"])


@app.get("/reverse-pickups", response_class=HTMLResponse)
async def rp_queue(request: Request, user: dict = Depends(get_current_user)):
    return render("pages/ow_rp_queue.html", request, user, board=rpl.board(track=True),
                  bucket_labels=rpl.BUCKETS)


@app.post("/orders/ow/{order_id}/return-receipt")
async def ow_return_receipt(request: Request, order_id: str,
                            awb: str = Form(""), courier: str = Form(""),
                            parcel_condition: str = Form(""),
                            return_reason: str = Form(""),
                            remarks: str = Form(""),
                            user: dict = Depends(get_current_user)):
    """Return Received: the parcel is physically at Ops. The receipt is saved
    locally first, then Optiwar is told via return-received (which triggers its
    customer notification and the Rs 250 reship option). If that call fails the
    receipt stands and a Retry Sync is offered; nobody re-receives the parcel."""
    if not ow_orders.get_order(order_id):
        raise HTTPException(status_code=404, detail="Order not found")
    try:
        rid = reship_svc.save_receipt(order_id, awb, courier, parcel_condition,
                                      return_reason, remarks, user["id"])
    except ValueError as e:
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?error={quote(str(e))}", status_code=302)
    auth.log_activity(user["id"], "return_receipt", "optiwar_order", 0,
        {"order": order_id, "awb": awb, "condition": parcel_condition}, request.client.host)
    st = reship_svc.status_for(order_id, user["id"], context="receive")
    if not st["error"] and st["status"] != "RETURNING":
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}?msg=" + quote(
                "Receipt note saved locally. Optiwar shows %s - nothing to confirm there."
                % (st["status"] or "no return in progress")), status_code=302)
    sync = reship_svc.confirm_return_received(order_id, user, awb, courier, return_reason, rid)
    if sync and sync["state"] == "synced":
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}?msg=" + quote(
                "Return received. Optiwar synchronized - customer is being notified; awaiting Rs 250 payment."),
            status_code=302)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(
            "Return received locally, but Optiwar sync failed (%s). Use Retry Sync - do not re-receive."
            % ((sync or {}).get("last_error") or "no response")), status_code=302)


@app.post("/orders/ow/{order_id}/reship-sync/{sync_id}/retry")
async def ow_reship_sync_retry(request: Request, order_id: str, sync_id: int,
                               user: dict = Depends(get_current_user)):
    row = reship_svc.get_sync(sync_id)
    if not row or row["ow_order_id"] != order_id:
        raise HTTPException(status_code=404, detail="Sync record not found")
    row = rpl.after_sync(reship_svc.run_sync(sync_id))
    auth.log_activity(user["id"], "reship_sync_retry", "optiwar_order", 0,
        {"order": order_id, "kind": row["kind"], "state": row["state"]}, request.client.host)
    back = request.headers.get("referer") or f"{ROOT_PATH}/orders/ow/{order_id}"
    sep = "&" if "?" in back else "?"
    if row["state"] == "synced":
        return RedirectResponse(back + sep + "msg=" + quote("Optiwar synchronized (%s)." % row["kind"]),
                                status_code=302)
    return RedirectResponse(back + sep + "error=" + quote(
        "Optiwar sync still failing: %s" % (row["last_error"] or "no response")), status_code=302)


@app.post("/api/optiwar/reship-webhook")
async def optiwar_reship_webhook(request: Request):
    """Optiwar pushes reship lifecycle events here (PAID / RESHIPPED /
    ABANDONED ...), HMAC-signed with the shared secret. Verified on the raw
    body, stored once per event_id; the queue API remains the full-state
    authority and is re-read on every new event."""
    body = await request.body()
    if not reship_svc.webhook_secret():
        return JSONResponse({"ok": False, "error": "webhook_not_configured"}, status_code=503)
    if not reship_svc.verify_webhook(body, request.headers.get("X-Optiwar-Signature", "")):
        return JSONResponse({"ok": False, "error": "bad_signature"}, status_code=401)
    try:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("object expected")
        stored, event_id = reship_svc.record_webhook(
            payload, (request.client.host if request.client else ""),
            request.headers.get("X-Optiwar-Event-Id", ""))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": "bad_payload", "message": str(e)},
                            status_code=400)
    ev = reship_svc.webhook_event(event_id)
    if ev and ev["event"].startswith(reship_svc.RP_EVENT_PREFIX) and not ev["processed_at"]:
        try:
            rpl.apply_webhook(json.loads(ev["payload"]))
            reship_svc.mark_webhook_processed(event_id)
        except Exception as e:
            logging.exception("reverse-pickup webhook %s not processed", event_id)
            return JSONResponse({"ok": False, "error": "processing_failed", "event_id": event_id,
                                 "message": str(e)[:200]}, status_code=500)
    if stored:
        auth.log_activity(None, "optiwar_reship_webhook", "optiwar_order", 0,
                          {"event": payload.get("event"), "event_id": event_id,
                           "order": payload.get("order_ref") or payload.get("order_id")},
                          request.client.host if request.client else "")
    return JSONResponse({"ok": True, "event_id": event_id, "duplicate": not stored})


@app.get("/returns", response_class=HTMLResponse)
async def returns_board(request: Request, user: dict = Depends(get_current_user)):
    board = reship_svc.returns_board(user["id"]) if reship_svc.configured() \
        else {"error": "OPS_API_TOKEN not set", "rows": [], "buckets": {k: [] for k, _ in reship_svc.BUCKETS}}
    return render("pages/ow_returns.html", request, user, board=board,
                  bucket_labels=reship_svc.BUCKETS, webhooks=reship_svc.recent_webhooks(),
                  webhook_configured=bool(reship_svc.webhook_secret()),
                  msg=request.query_params.get("msg"),
                  error=request.query_params.get("error"))


@app.get("/orders/ow/{order_id}/ship", response_class=HTMLResponse)
async def ow_ship_form(request: Request, order_id: str,
                       courier: str = Query("Delhivery"),
                       reship: int = Query(0),
                       rp: int = Query(0),
                       user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    courier = _norm_courier(courier)
    reship_ref = ""
    rp_pickup = None
    if rp:
        rp_pickup = _rp_pickup(order_id, rp)
        allowed, why = rpl.forward_allowed(rp)
        if not allowed:
            return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(why) + "#rpCard",
                                    status_code=302)
        reship = 0
    if reship:
        st = reship_svc.status_for(order_id, user["id"], context="form")
        if not st["paid"]:
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(
                    "Reship refused: Optiwar shows %s, not PAID%s" % (
                        st["status"] or "no reship entry",
                        (" (" + st["error"] + ")") if st["error"] else "")),
                status_code=302)
        done = reship_svc.booked_for(order_id, st)
        if done:
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(
                    "Reship already booked: %s %s - use Retry Sync on the order page if Optiwar has not received it"
                    % (done["new_courier"], done["new_awb"])), status_code=302)
        reship_ref = reship_svc.next_reference(order_id)
    serviceable = None
    try:
        pin = str(order.get("shipping_postcode", "")).strip()
        if courier == "Delhivery" and pin and config.DELHIVERY_API_TOKEN:
            serviceable = delhivery_svc.check_serviceability(pin)
    except Exception:
        serviceable = None
    return render("pages/ow_ship_order.html", request, user, order=order,
                  serviceable=serviceable, courier=courier,
                  dtdc_configured=dtdc_svc.configured(),
                  reship=bool(reship), reship_ref=reship_ref, rp_pickup=rp_pickup,
                  dtdc_services={"Surface": config.DTDC_SERVICE_SURFACE, "Express": config.DTDC_SERVICE_EXPRESS}, dtdc_live=config.DTDC_LIVE)


def _norm_courier(value: str) -> str:
    v = (value or "").strip().lower()
    return "DTDC" if v == "dtdc" else "Delhivery"


def _latest_awb(order_id: str, full: bool = False):
    cols = "*" if full else "tracking_number, courier, awb_status"
    with ow_cursor() as cur:
        cur.execute(f"""SELECT {cols} FROM ops_shipping_awb
                       WHERE ow_order_id = %s AND awb_status != 'cancelled'
                       ORDER BY id DESC LIMIT 1""", (order_id,))
        return cur.fetchone()


@app.post("/orders/ow/{order_id}/ship")
async def ow_ship_order(request: Request, order_id: str,
                        courier: str = Form("Delhivery"),
                        service_type: str = Form(""),
                        shipping_mode: str = Form("Surface"),
                        payment_mode: str = Form("Prepaid"),
                        weight_kg: float = Form(0.25),
                        length_cm: int = Form(17),
                        width_cm: int = Form(17),
                        height_cm: int = Form(7),
                        total_amount: float = Form(0),
                        cod_amount: float = Form(0),
                        qty: int = Form(0),
                        products_desc: str = Form(""),
                        change_item: List[str] = Form(default=[]),
                        change_to: List[str] = Form(default=[]),
                        change_approved: List[str] = Form(default=[]),
                        change_note: str = Form(""),
                        reship: int = Form(0),
                        rp: int = Form(0),
                        user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    changes = _build_changes(change_item, change_to, change_approved)
    courier = _norm_courier(courier)
    rp_pickup = None
    if rp:
        rp_pickup = _rp_pickup(order_id, rp)
        allowed, why = rpl.forward_allowed(rp)
        if not allowed:
            return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(why) + "#rpCard",
                                    status_code=302)
        reship = 0
        changes = []

    # Reship of a returned parcel: PAID is re-checked at booking time from
    # Optiwar's queue API; the original AWB row is kept and marked 'returned'.
    reship_state = None
    reship_ref = ""
    original_awb = None
    if reship:
        reship_state = reship_svc.status_for(order_id, user["id"], context="book")
        if not reship_state["paid"]:
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(
                    "Reship refused: Optiwar shows %s, not PAID" % (reship_state["status"] or "no entry")),
                status_code=302)
        done = reship_svc.booked_for(order_id, reship_state)
        if done:
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}?error=" + quote(
                    "Reship already booked: %s %s - use Retry Sync on the order page if Optiwar has not received it"
                    % (done["new_courier"], done["new_awb"])), status_code=302)
        reship_ref = reship_svc.next_reference(order_id)
        original_awb = _latest_awb(order_id, full=True)
        changes = []

    if shipping_mode not in ("Surface", "Express"):
        shipping_mode = "Surface"
    if payment_mode not in ("Prepaid", "COD"):
        payment_mode = "Prepaid"

    params = {
        "shipping_mode": shipping_mode,
        "payment_mode": payment_mode,
        "weight_kg": weight_kg,
        "length_cm": length_cm, "width_cm": width_cm, "height_cm": height_cm,
        "total_amount": total_amount or order.get("total_inr", 0),
        "cod_amount": cod_amount,
        "qty": qty or None,
        "products_desc": products_desc.strip() or None,
        "service_type": service_type.strip() or None,
    }
    if reship_ref:
        params["reference"] = reship_ref
    if rp_pickup:
        params["reference"] = rp_pickup["reference"] + "-F"

    def _form_error(msg, details=None):
        return render("pages/ow_ship_order.html", request, user, order=order,
                      error=msg, error_details=details, serviceable=None,
                      reship=bool(reship), reship_ref=reship_ref, rp_pickup=rp_pickup,
                      courier=courier, dtdc_configured=dtdc_svc.configured(),
                      dtdc_services={"Surface": config.DTDC_SERVICE_SURFACE, "Express": config.DTDC_SERVICE_EXPRESS}, dtdc_live=config.DTDC_LIVE)

    try:
        if courier == "DTDC":
            if not dtdc_svc.configured():
                return _form_error("DTDC is not configured yet: DTDC_API_KEY / DTDC_CUSTOMER_CODE "
                                   "missing from the server .env. Use Delhivery or add the key.")
            resp = dtdc_svc.create_consignment(order, params)
            waybill = dtdc_svc.extract_cn(resp)
            label_path = dtdc_svc.save_label_pdf(waybill, order_id) if waybill else ""
        else:
            resp = delhivery_svc.create_waybill(order, params)
            waybill = delhivery_svc.extract_waybill(resp)
            label_path = delhivery_svc.save_label_pdf(waybill, order_id) if waybill else ""

        with ow_ops_cursor() as cur:
            cur.execute("""
                INSERT INTO ops_shipping_awb (ow_order_id, tracking_number,
                    courier, awb_status, label_pdf_path, courier_response, created_by)
                VALUES (%s, %s, %s, 'created', %s, %s, %s)
            """, (order_id, waybill, courier, label_path or None, json.dumps(resp), user["id"]))
            new_row_id = cur.lastrowid
            if reship and original_awb:
                cur.execute("UPDATE ops_shipping_awb SET awb_status = 'returned' "
                            "WHERE id = %s AND awb_status = 'created'", (original_awb["id"],))

        auth.log_activity(user["id"], "reship_awb_created" if reship else "awb_created",
            "optiwar_order", 0,
            {"order": order_id, "tracking": waybill, "mode": shipping_mode, "courier": courier,
             "reference": reship_ref or None,
             "original": (original_awb or {}).get("tracking_number")},
            request.client.host)

        if rp_pickup:
            # A new forward shipment after a reverse pickup: original and reverse
            # AWBs stay as they are; no Ops status/history/customer message -
            # Optiwar's forward-shipped call does that once Ops confirms handover.
            rpl.record_forward(rp_pickup["id"], courier, waybill, new_row_id, user)
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}?msg=" + quote(
                    "Forward %s AWB %s booked. Press Confirm Dispatch when the parcel is handed to "
                    "the courier - that syncs Optiwar." % (courier, waybill)) + "#rpCard",
                status_code=302)

        if reship:
            rid = reship_svc.record(order_id, reship_ref, original_awb, new_row_id,
                                    waybill, courier, reship_state, user["id"])
            # No Shipped status, no history note, no customer email/WhatsApp from
            # here: Optiwar's /ship does all of that once it receives the AWB.
            ruuid = str((reship_state.get("entry") or {}).get("reship_uuid") or "")
            sync = reship_svc.sync_ship(order_id, rid, ruuid, courier, waybill, user)
            if sync and sync["state"] == "synced":
                return RedirectResponse(
                    f"{ROOT_PATH}/orders/ow/{order_id}?reship_done={rid}&msg=" + quote(
                        "Reship %s AWB %s booked and synced to Optiwar (RESHIPPED)." % (courier, waybill)),
                    status_code=302)
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}?reship_done={rid}&error=" + quote(
                    "Courier booked (%s %s) but Optiwar sync failed: %s. Use Retry Sync - do not book again."
                    % (courier, waybill, (sync or {}).get("last_error") or "no response")),
                status_code=302)

        # Persist approved order changes back into optiwar2 rx_collector (with audit timestamp)
        try:
            if changes:
                ow_orders.apply_rx_changes(order, changes, changed_by=user["id"])
        except Exception as rex:
            logging.warning("apply_rx_changes failed for %s: %s", order_id, rex)

        # Write 'Shipped' status + history back to optiwar2
        try:
            ow_orders.mark_shipped(order_id, waybill, courier=courier, changed_by=user["id"])
        except Exception as se:
            logging.warning("mark_shipped failed for %s: %s", order_id, se)

        # Reload so the email reflects the updated selection (post-change) and new status
        order = ow_orders.get_order(order_id) or order

        # Notify customer (email from admin@optiwar.com + WhatsApp via msg91)
        notify_note = ""
        try:
            eta_days = "2-3" if shipping_mode.strip().lower() == "express" else "5-7"
            nres = notifications.notify_ow_shipped(order, waybill, courier=courier,
                                                   changes=changes, note=change_note.strip(),
                                                   eta_days=eta_days)
            ok = nres.get("email", (False, ""))[0]
            notify_note = "&notify=sent" if ok else "&notify=failed"
        except Exception as ne:
            logging.warning("Optiwar shipped notification failed for %s: %s", order_id, ne)
            notify_note = "&notify=failed"

        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}?msg={courier}+AWB+created:+{waybill}{notify_note}",
            status_code=302)

    except (delhivery_svc.DelhiveryError, dtdc_svc.DtdcError) as e:
        return _form_error(str(e), e.details)
    except Exception as e:
        return _form_error(f"Unexpected error: {str(e)}")


@app.get("/awb/ow/{awb_id}/label")
async def ow_download_label(awb_id: int, user: dict = Depends(get_current_user)):
    with ow_cursor() as cur:
        cur.execute("SELECT * FROM ops_shipping_awb WHERE id = %s", (awb_id,))
        awb = cur.fetchone()
    if not awb:
        raise HTTPException(404, "AWB not found")
    path = awb.get("label_pdf_path") or ""
    if not (path and os.path.exists(path)):
        if _norm_courier(awb.get("courier")) == "DTDC":
            path = dtdc_svc.save_label_pdf(awb["tracking_number"], awb["ow_order_id"])
        else:
            path = delhivery_svc.save_label_pdf(awb["tracking_number"], awb["ow_order_id"])
        if not path:
            raise HTTPException(404, "Label not available from courier yet — try again in a minute")
        with ow_ops_cursor() as cur:
            cur.execute("UPDATE ops_shipping_awb SET label_pdf_path = %s WHERE id = %s", (path, awb_id))
    return FileResponse(path, filename=f"AWB_{awb['tracking_number']}.pdf",
                        media_type="application/pdf")


@app.post("/awb/ow/{awb_id}/cancel")
async def ow_cancel_awb(request: Request, awb_id: int, user: dict = Depends(get_current_user)):
    with ow_cursor() as cur:
        cur.execute("SELECT * FROM ops_shipping_awb WHERE id = %s", (awb_id,))
        awb = cur.fetchone()
    if not awb:
        raise HTTPException(404)
    try:
        if _norm_courier(awb.get("courier")) == "DTDC":
            dtdc_svc.cancel_consignment(awb["tracking_number"])
        else:
            delhivery_svc.cancel_waybill(awb["tracking_number"])
        with ow_ops_cursor() as cur:
            cur.execute("UPDATE ops_shipping_awb SET awb_status = 'cancelled' WHERE id = %s", (awb_id,))
        auth.log_activity(user["id"], "awb_cancelled", "optiwar_order", 0,
            {"order": awb["ow_order_id"], "tracking": awb["tracking_number"]}, request.client.host)
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{awb['ow_order_id']}?msg=AWB+cancelled", status_code=302)
    except (delhivery_svc.DelhiveryError, dtdc_svc.DtdcError) as e:
        return RedirectResponse(f"{ROOT_PATH}/orders/ow/{awb['ow_order_id']}?error={str(e)}", status_code=302)


def _build_changes(items, tos, approveds):
    """Zip operator-entered order-change rows into dicts, skipping empty rows."""
    changes = []
    items = items or []
    tos = tos or []
    approveds = approveds or []
    for i in range(max(len(items), len(tos))):
        item = (items[i] if i < len(items) else "").strip()
        to = (tos[i] if i < len(tos) else "").strip()
        appr = (approveds[i] if i < len(approveds) else "").strip() or "Yes"
        if item or to:
            changes.append({"item": item, "changed_to": to, "approved": appr})
    return changes


def _resolve_preview_tracking(order_id, tracking):
    """Returns (tracking, courier) for the notification dry-run."""
    with ow_cursor() as cur:
        cur.execute("""SELECT tracking_number, courier FROM ops_shipping_awb
                       WHERE ow_order_id = %s AND tracking_number IS NOT NULL
                       ORDER BY id DESC LIMIT 1""", (order_id,))
        row = cur.fetchone()
    courier = _norm_courier(row.get("courier")) if row else "Delhivery"
    if tracking:
        return tracking, courier
    return ((row["tracking_number"] if row else None) or "SAMPLE0000000000"), courier


@app.get("/orders/ow/{order_id}/notify-preview", response_class=HTMLResponse)
async def ow_notify_preview(request: Request, order_id: str,
                            tracking: str = Query(None),
                            user: dict = Depends(get_current_user)):
    """Dry run (quick): show the dispatch email with no order changes — nothing is sent."""
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    tracking, courier = _resolve_preview_tracking(order_id, tracking)
    preview = notifications.notify_ow_shipped(order, tracking, courier=courier, dry_run=True)
    return render("pages/ow_notify_preview.html", request, user, order=order, preview=preview)


@app.post("/orders/ow/{order_id}/notify-preview", response_class=HTMLResponse)
async def ow_notify_preview_post(request: Request, order_id: str,
                                 tracking: str = Form(None),
                                 courier: str = Form(""),
                                 change_item: List[str] = Form(default=[]),
                                 change_to: List[str] = Form(default=[]),
                                 change_approved: List[str] = Form(default=[]),
                                 change_note: str = Form(""),
                                 user: dict = Depends(get_current_user)):
    """Dry run: preview the dispatch email built from operator-entered order changes.
    Sends nothing."""
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    tracking, awb_courier = _resolve_preview_tracking(order_id, tracking)
    courier = _norm_courier(courier) if courier else awb_courier
    changes = _build_changes(change_item, change_to, change_approved)
    preview = notifications.notify_ow_shipped(order, tracking, courier=courier,
                                              changes=changes, note=change_note.strip(),
                                              dry_run=True)
    return render("pages/ow_notify_preview.html", request, user, order=order, preview=preview)


@app.post("/orders/ow/{order_id}/status")
async def ow_update_status(request: Request, order_id: str,
                           status_id: str = Form(...),
                           comment: str = Form(""),
                           notify_email: str = Form(None),
                           notify_whatsapp: str = Form(None),
                           notify_sms: str = Form(None),
                           user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    ow_orders.update_status(order_id, status_id, comment.strip(), changed_by=user["id"])

    channels = []
    if notify_email:
        channels.append("email")
    if notify_whatsapp:
        channels.append("whatsapp")
    if notify_sms:
        channels.append("sms")
    if channels:
        order = ow_orders.get_order(order_id) or order
        tracking, courier = "", "Delhivery"
        row = _latest_awb(order_id)
        if row:
            tracking = row["tracking_number"] or ""
            courier = _norm_courier(row.get("courier"))
        try:
            notifications.notify_ow_status(order, status_id, comment=comment.strip(),
                                           channels=channels, tracking=tracking,
                                           courier=courier)
        except Exception as ne:
            logging.warning("Optiwar status notification failed for %s: %s", order_id, ne)
    return RedirectResponse(f"{ROOT_PATH}/orders/ow/{order_id}?msg=Status+updated+to+{status_id}",
                            status_code=302)


@app.get("/orders/ow/{order_id}/invoice", response_class=HTMLResponse)
async def ow_order_invoice(request: Request, order_id: str, user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    invoice = ow_orders.get_invoice(order_id)
    if not invoice:
        return _invoice_issue_prompt(request, f"OW-{order_id}", order,
                                     f"{ROOT_PATH}/orders/ow/{order_id}",
                                     f"{ROOT_PATH}/orders/ow/{order_id}/invoice/issue?next=invoice")
    inv_dt = invoice["invoice_date"]
    return templates.TemplateResponse(request=request, name="pages/ow_invoice.html",
        context={"request": request, "order": order, "base": ROOT_PATH,
                 "invoice_date": invoice_dates.fmt(inv_dt),
                 "invoice_no": invoice.get("invoice_no") or invoice_series.legacy_ow(order_id)})


def _invoice_issue_prompt(request: Request, order_label: str, order: dict, back_url: str,
                          action_url: str, error: str = "", entered: str = ""):
    today = dt.date.today()
    od = order.get("date_added")
    od_date = od.date() if isinstance(od, datetime) else (od if isinstance(od, dt.date) else None)
    return templates.TemplateResponse(request=request, name="pages/invoice_issue_date.html",
        context={"request": request, "base": ROOT_PATH, "order_label": order_label,
                 "order_date": invoice_dates.fmt(od), "today": invoice_dates.fmt(today),
                 "min_iso": od_date.isoformat() if od_date else "", "max_iso": today.isoformat(),
                 "back_url": back_url, "action_url": action_url,
                 "error": error, "entered": entered},
        status_code=400 if error else 200)


@app.post("/orders/ow/{order_id}/invoice/issue")
async def ow_invoice_issue(request: Request, order_id: str,
                           next: str = Query(default="invoice"),
                           choice: str = Form(""), print_date: str = Form(""),
                           user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    target = "gst-invoice" if next == "gst-invoice" else "invoice"
    dest = f"{ROOT_PATH}/orders/ow/{order_id}/{target}"
    if ow_orders.get_invoice_date(order_id):
        return RedirectResponse(dest, status_code=303)
    issue_date, source, error = invoice_dates.resolve_choice(
        choice, print_date, order.get("date_added"))
    if error:
        return _invoice_issue_prompt(request, f"OW-{order_id}", order,
                                     f"{ROOT_PATH}/orders/ow/{order_id}",
                                     f"{ROOT_PATH}/orders/ow/{order_id}/invoice/issue?next={target}",
                                     error=error, entered=print_date)
    stamp = (datetime.now().replace(microsecond=0) if source == "today"
             else datetime.combine(issue_date, dt.time()))
    if ow_orders.issue_invoice_date(order_id, stamp, changed_by=user["id"]):
        invoice = ow_orders.get_invoice(order_id)
        auth.log_activity(user["id"], "invoice_issued", "ow_order", None,
                          {"order_id": order_id, "invoice_date": issue_date.isoformat(),
                           "source": source,
                           "invoice_no": invoice.get("invoice_no") if invoice else None},
                          request.client.host)
    return RedirectResponse(dest, status_code=303)


def _lens_design_label(text: str) -> str:
    """Map a lens add-on / recommendation string to a catalogue lens-design label
    for the tax-invoice line title. Extend the list as optiwar.in adds designs."""
    t = (text or "").lower()
    # order matters — most specific first
    checks = [
        ("flat-top", "Biofocal Flat-top D"), ("flat top", "Biofocal Flat-top D"),
        ("round top", "Biofocal Round Top"), ("round-top", "Biofocal Round Top"),
        ("progressive", "Progressive"),
        ("bifocal", "Biofocal"), ("biofocal", "Biofocal"),
        ("single", "Single"),
    ]
    for kw, label in checks:
        if kw in t:
            return label
    return ""


def _build_gst_invoice(order: dict):
    """Build a GST tax-invoice view: one Spectacle Frames line + one consolidated
    Spectacle Lenses line (design in the title, coatings listed as included), all
    at actual GST-inclusive prices, GST extracted per line. Frame amount = charged
    total − add-ons, so lines reconcile exactly to the charged total."""
    rate = config.OPTIWAR_GST_RATE
    seller_state = (config.OPTIWAR_SELLER_STATE or "").strip().lower()
    buyer_state = (order.get("shipping_zone") or "").strip()
    intra = seller_state and buyer_state.strip().lower() == seller_state
    items = []
    tot_taxable = tot_cgst = tot_sgst = tot_igst = tot_gross = tot_gst = 0.0
    for p in order.get("products", []):
        line_total = float(p.get("total") or 0)
        rx = p.get("rx") or {}
        # add-ons at their actual (GST-inclusive) prices
        addons = []
        for i in (1, 2, 3):
            nm = (rx.get(f"addon_{i}_name") or "").strip()
            pr = float(rx.get(f"addon_{i}_price") or 0)
            if nm and pr > 0:
                addons.append({"name": nm, "gross": round(pr, 2),
                               "design": _lens_design_label(nm)})
        addon_sum = round(sum(a["gross"] for a in addons), 2)
        # frame = charged total minus add-ons (= product selling price, e.g. ₹599)
        frame_gross = round(line_total - addon_sum, 2)
        if frame_gross < 0:  # data guard: never show a negative frame line
            frame_gross = round(line_total, 2)

        alloc = [{"desc": "Spectacle Frames", "code": p.get("model") or "",
                  "hsn": config.OPTIWAR_HSN_FRAME, "included": [], "gross": frame_gross}]

        if addons:
            # detect lens design from an add-on, else from the recommendations text
            design = next((a["design"] for a in addons if a["design"]), "") \
                or _lens_design_label(rx.get("recommendations") or "")
            design_addon = next((a for a in addons if a["design"] == design and design), None)
            included = [a["name"] for a in addons if a is not design_addon]
            lens_desc = "Spectacle Lenses — " + design if design else "Spectacle Lenses"
            alloc.append({"desc": lens_desc, "code": "", "hsn": config.OPTIWAR_HSN_LENS,
                          "included": included, "gross": addon_sum})

        for c in alloc:
            gross = c["gross"]
            taxable = round(gross / (1 + rate / 100.0), 2)
            gst = round(gross - taxable, 2)
            row = {"desc": c["desc"], "code": c["code"], "hsn": c["hsn"],
                   "included": c["included"],
                   "qty": p.get("quantity", 1), "gross": gross, "taxable": taxable,
                   "rate": rate, "gst": gst,
                   "cgst": round(gst / 2, 2) if intra else 0.0,
                   "sgst": round(gst / 2, 2) if intra else 0.0,
                   "igst": 0.0 if intra else gst}
            items.append(row)
            tot_taxable += taxable; tot_gross += gross; tot_gst += gst
            tot_cgst += row["cgst"]; tot_sgst += row["sgst"]; tot_igst += row["igst"]

    # Free lens upgrade: the customer opted for complimentary lenses (already covered
    # by the frame line), so we do NOT add a separate lens line item. The upgrade is
    # shown only via the complimentary-upgrade callout below. Totals are unchanged.
    upg = order.get("upgrade")
    upgrade = None
    if upg and (upg.get("to_lens") or "").strip() and (upg.get("status") or "applied") != "withdrawn":
        to_lens = (upg.get("to_lens") or "").strip()
        from_lens = (upg.get("from_lens") or "").strip()
        upgrade = {"from_lens": from_lens, "to_lens": to_lens,
                   "note": (upg.get("note") or "").strip()}

    return {
        "lines": items, "intra": intra, "rate": rate,
        "buyer_state": buyer_state or "—", "seller_state": config.OPTIWAR_SELLER_STATE,
        "gstin": config.OPTIWAR_GSTIN, "upgrade": upgrade,
        "totals": {"taxable": round(tot_taxable, 2), "cgst": round(tot_cgst, 2),
                   "sgst": round(tot_sgst, 2), "igst": round(tot_igst, 2),
                   "gst": round(tot_gst, 2), "gross": round(tot_gross, 2)},
    }


@app.get("/orders/ow/{order_id}/gst-invoice", response_class=HTMLResponse)
async def ow_order_gst_invoice(request: Request, order_id: str,
                               user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    invoice = ow_orders.get_invoice(order_id)
    if not invoice:
        return _invoice_issue_prompt(request, f"OW-{order_id}", order,
                                     f"{ROOT_PATH}/orders/ow/{order_id}",
                                     f"{ROOT_PATH}/orders/ow/{order_id}/invoice/issue?next=gst-invoice")
    inv_dt = invoice["invoice_date"]
    gst = _build_gst_invoice(order)
    invoice_date = invoice_dates.fmt(inv_dt)
    # Once an AWB is issued show courier + tracking on the invoice footer.
    shipment = None
    awb = _latest_awb(order_id)
    if awb and awb.get("tracking_number"):
        c = _norm_courier(awb.get("courier"))
        shipment = {
            "courier": c,
            "tracking_number": awb["tracking_number"],
            "tracking_url": config.courier_track_url(c, awb["tracking_number"]),
        }
    return templates.TemplateResponse(request=request, name="pages/ow_invoice_gst.html",
        context={"request": request, "order": order, "gst": gst, "base": ROOT_PATH,
                 "invoice_date": invoice_date, "shipment": shipment,
                 "invoice_no": invoice.get("invoice_no") or invoice_series.legacy_ow(order_id)})


@app.get("/orders/ow/{order_id}/support-email", response_class=HTMLResponse)
async def ow_support_email_page(request: Request, order_id: str,
                                notes: str = Query(default=""),
                                followup: int = Query(default=0),
                                user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    previous = notifications.parse_email_details(
        notifications.get_notification_log(order_id, followup)) if followup else {}
    draft = None
    error = None
    if notes.strip():
        try:
            draft = support_email.draft_ow_support_email(order, notes.strip(),
                                                         previous=previous or None)
            import html as html_mod
            draft["body_html_escaped"] = html_mod.escape(draft["body_html"])
        except Exception as e:
            logging.error("OW support email draft failed for %s: %s", order_id, e)
            error = str(e)
    return render("pages/ow_support_email_draft.html", request, user,
                  order=order, draft=draft, agent_notes=notes, error=error,
                  previous=previous, followup_id=followup if previous else 0)


@app.post("/orders/ow/{order_id}/support-email/send")
async def ow_support_email_send(request: Request, order_id: str,
                                subject: str = Form(...),
                                body_html: str = Form(...),
                                to: str = Form(...),
                                agent_notes: str = Form(""),
                                extra_cc: str = Form(""),
                                followup_id: int = Form(0),
                                user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    import html as html_mod
    body_unescaped = html_mod.unescape(body_html)

    cc_list = []
    if extra_cc.strip():
        for addr in extra_cc.split(","):
            addr = addr.strip()
            if addr and "@" in addr and addr not in cc_list:
                cc_list.append(addr)
    extra_cc_str = ", ".join(cc_list) if cc_list else None

    ok, detail = notifications.send_email(
        to, subject, body_unescaped,
        cc=extra_cc_str,
        from_addr=config.OPTIWAR_FROM_EMAIL,
        admin_cc=config.OPTIWAR_ADMIN_CC or None,
        smtp_host=config.OPTIWAR_SMTP_HOST or None,
        smtp_port=config.OPTIWAR_SMTP_PORT or None,
        smtp_user=config.OPTIWAR_SMTP_USER or None,
        smtp_pass=config.OPTIWAR_SMTP_PASS or None)

    all_cc = ([config.OPTIWAR_ADMIN_CC] if config.OPTIWAR_ADMIN_CC else []) + cc_list
    kind = "Follow-up support email" if followup_id else "Support email"
    log_id = notifications._save_notification_log(
        order_id, "email", "support_email", to, ok,
        (f"{kind} sent. CC: {', '.join(all_cc)}. Subject: {subject}" if ok else str(detail)),
        user["username"],
        details=notifications.email_details(subject, body_unescaped, followup_of=followup_id))
    if not ok:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}?error=Email+send+failed:+{str(detail)[:80]}",
            status_code=302)
    # order_history is latin1: keep the line ASCII-safe.
    hist = f"{kind} sent to {to} - Subject: {subject}"
    if log_id:
        hist += f" (notification #{log_id}, full text on record)"
    ow_orders.add_history(order_id, hist.encode("ascii", "replace").decode("ascii"))
    auth.log_activity(user["id"], "ow_support_email_sent", "order", 0,
        {"order_id": order_id, "to": to, "cc": all_cc, "subject": subject,
         "notes": agent_notes[:200], "notification_id": log_id,
         "followup_of": followup_id or None}, request.client.host)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{order_id}?msg=Support+email+sent+to+{to}",
        status_code=302)


@app.get("/orders/ow/{order_id}/support-email/view/{log_id}", response_class=HTMLResponse)
async def ow_support_email_view(request: Request, order_id: str, log_id: int,
                                user: dict = Depends(get_current_user)):
    row = notifications.get_notification_log(order_id, log_id)
    if not row or row.get("channel") != "email":
        raise HTTPException(status_code=404, detail="Email log not found")
    return render("pages/support_email_view.html", request, user,
                  row=row, email=notifications.parse_email_details(row),
                  order_label=f"OW-{order_id}",
                  back_url=f"{ROOT_PATH}/orders/ow/{order_id}",
                  view_base=f"{ROOT_PATH}/orders/ow/{order_id}/support-email/view",
                  followup_url=f"{ROOT_PATH}/orders/ow/{order_id}/support-email?followup={log_id}")


@app.get("/orders/ow/{order_id}/upgrade", response_class=HTMLResponse)
async def ow_upgrade_page(request: Request, order_id: str,
                          from_lens: str = Query(default=""),
                          to_lens: str = Query(default=""),
                          note: str = Query(default=""),
                          channels: str = Query(default="email,whatsapp"),
                          edit: int = Query(default=0),
                          user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    upg = order.get("upgrade")
    ulog = ow_orders.get_upgrade_log(order_id)
    # Real EWS delivery receipts (proof the customer was actually notified).
    try:
        notif_log = [r for r in (notifications.get_notification_logs(order_id) or [])
                     if str(r.get("event", "")).startswith("ow_upgrade")]
    except Exception:
        notif_log = []
    locked = bool(upg and upg.get("locked"))
    active = bool(upg and (upg.get("status") or "applied") == "applied")

    # Prefill the "original lens": existing upgrade's value, else rx recommendations.
    suggested_from = from_lens.strip()
    if not suggested_from and upg and (upg.get("from_lens") or "").strip():
        suggested_from = (upg.get("from_lens") or "").strip()
    if not suggested_from:
        for p in order.get("products", []):
            rec = ((p.get("rx") or {}).get("recommendations") or "").strip()
            if rec:
                suggested_from = rec
                break

    # Decide the screen: a locked upgrade can't be changed (must unlock first).
    if locked:
        mode = "status"
    elif to_lens.strip():
        mode = "preview"
    elif active and not edit:
        mode = "status"
    else:
        mode = "form"

    draft = None
    prefill_to = to_lens.strip()
    prefill_note = note.strip()
    if mode == "form" and active and not prefill_to:
        prefill_to = (upg.get("to_lens") or "").strip()
        prefill_note = (upg.get("note") or "").strip()
    if mode == "preview":
        chans = [c for c in channels.split(",") if c.strip()]
        preview = notifications.notify_ow_upgrade(
            order, from_lens=suggested_from, to_lens=to_lens.strip(),
            note=note.strip(), channels=chans, dry_run=True)
        import html as html_mod
        preview["body_html_escaped"] = html_mod.escape(preview["html"])
        draft = preview
    return render("pages/ow_upgrade.html", request, user,
                  order=order, draft=draft, mode=mode, upgrade=upg,
                  upgrade_log=ulog, notif_log=notif_log, suggested_from=suggested_from,
                  to_lens=prefill_to, note=prefill_note, channels=channels)


@app.post("/orders/ow/{order_id}/upgrade/lock")
async def ow_upgrade_lock(request: Request, order_id: str,
                          user: dict = Depends(get_current_user)):
    if not ow_orders.get_order(order_id):
        raise HTTPException(status_code=404, detail="Order not found")
    ow_orders.lock_upgrade(order_id, actor_id=user["id"])
    auth.log_activity(user["id"], "ow_upgrade_lock", "order", 0,
                      {"order_id": order_id}, request.client.host)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{order_id}/upgrade?msg=Upgrade+locked", status_code=302)


@app.post("/orders/ow/{order_id}/upgrade/unlock")
async def ow_upgrade_unlock(request: Request, order_id: str,
                            user: dict = Depends(get_current_user)):
    if not ow_orders.get_order(order_id):
        raise HTTPException(status_code=404, detail="Order not found")
    ow_orders.unlock_upgrade(order_id, actor_id=user["id"])
    auth.log_activity(user["id"], "ow_upgrade_unlock", "order", 0,
                      {"order_id": order_id}, request.client.host)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{order_id}/upgrade?edit=1&msg=Unlocked+for+editing",
        status_code=302)


@app.post("/orders/ow/{order_id}/upgrade/withdraw")
async def ow_upgrade_withdraw(request: Request, order_id: str,
                              reason: str = Form(""),
                              notify_customer: str = Form(None),
                              user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    upg = order.get("upgrade") or {}
    ow_orders.withdraw_upgrade(order_id, reason=reason, actor_id=user["id"])
    # Optional courtesy notification that the free upgrade was withdrawn.
    if notify_customer:
        try:
            to_lens = (upg.get("to_lens") or "").strip()
            res = notifications.notify_ow_upgrade_withdrawn(
                order, to_lens=to_lens, reason=reason,
                channels=["email", "whatsapp"]) or {}
            for ch, r in res.items():
                ok = bool(r[0]) if isinstance(r, (tuple, list)) and r else False
                ow_orders.log_upgrade_action(
                    order_id, "notified",
                    f"Withdrawal {'sent' if ok else 'failed'} via {ch}", user["id"])
        except Exception as we:
            logging.warning("withdraw notify failed for %s: %s", order_id, we)
    auth.log_activity(user["id"], "ow_upgrade_withdraw", "order", 0,
                      {"order_id": order_id, "reason": reason[:200]}, request.client.host)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{order_id}/upgrade?msg=Upgrade+withdrawn+and+reverted",
        status_code=302)


@app.post("/orders/ow/{order_id}/upgrade/send")
async def ow_upgrade_send(request: Request, order_id: str,
                          from_lens: str = Form(""),
                          to_lens: str = Form(...),
                          note: str = Form(""),
                          subject: str = Form(...),
                          body_html: str = Form(...),
                          notify_email: str = Form(None),
                          notify_whatsapp: str = Form(None),
                          notify_sms: str = Form(None),
                          user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    to_lens = (to_lens or "").strip()
    if not to_lens:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}?error=Upgrade+lens+is+required", status_code=302)

    import html as html_mod
    body_unescaped = html_mod.unescape(body_html)

    # Persist the upgrade first so the invoice reflects it even if a channel fails.
    ow_orders.set_upgrade(order_id, from_lens=from_lens, to_lens=to_lens,
                          note=note, changed_by=user["id"])
    order["upgrade"] = {"from_lens": from_lens, "to_lens": to_lens, "note": note}
    # A free upgrade is NOT a product/RX change — the customer's purchase stays as
    # ordered. It lives only in ow_order_upgrades and shows on the invoice; we do
    # NOT modify rx_collector.

    channels = []
    if notify_email:
        channels.append("email")
    if notify_whatsapp:
        channels.append("whatsapp")
    if notify_sms:
        channels.append("sms")

    results = {}
    if channels:
        try:
            results = notifications.notify_ow_upgrade(
                order, from_lens=from_lens, to_lens=to_lens, note=note,
                channels=channels, subject=subject, body_html=body_unescaped) or {}
        except Exception as ne:
            logging.warning("Optiwar upgrade notification failed for %s: %s", order_id, ne)

    label = {"email": "Email", "whatsapp": "WhatsApp", "sms": "SMS"}
    for ch in channels:
        ok = False
        ref = ""
        res = results.get(ch)
        if isinstance(res, (tuple, list)) and res:
            ok = bool(res[0])
            if len(res) > 1:
                ref = notifications._extract_reference(str(res[1]))
        state = "sent" if ok else "failed"
        note_line = f"Upgrade notification {state} via {label.get(ch, ch)}"
        if ref:
            note_line += f" (ref {ref})"
        ow_orders.add_history(order_id, note_line)

    # Once the upgrade is done + notified, lock it so the invoice is finalized.
    ow_orders.lock_upgrade(order_id, actor_id=user["id"])
    auth.log_activity(user["id"], "ow_free_upgrade", "order", 0,
        {"order_id": order_id, "from_lens": from_lens, "to_lens": to_lens,
         "channels": channels, "note": note[:200]}, request.client.host)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{order_id}/upgrade?msg=Upgrade+recorded,+locked+and+customer+notified",
        status_code=302)


@app.get("/orders/ow/{order_id}/partial-refund-preview", response_class=HTMLResponse)
async def ow_partial_refund_preview(request: Request, order_id: str,
                                    amount: str = Query(default=""),
                                    reason: str = Query(default=""),
                                    channels: str = Query(default="email"),
                                    user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    chans = [c for c in channels.split(",") if c.strip()]
    preview = notifications.notify_ow_partial_refund(
        order, amount=amount, reason=reason, channels=chans, dry_run=True)
    return templates.TemplateResponse(request=request, name="pages/ow_dry_run.html",
        context={"request": request, "order": order, "preview": preview,
                 "base": ROOT_PATH, "title": "Partial Refund — Dry Run"})


@app.post("/orders/ow/{order_id}/partial-refund")
async def ow_partial_refund(request: Request, order_id: str,
                            amount: str = Form(""),
                            reason: str = Form(""),
                            mode: str = Form("manual"),
                            notify_email: str = Form(None),
                            notify_whatsapp: str = Form(None),
                            notify_sms: str = Form(None),
                            notify_accounts: str = Form(None),
                            accounts_email: str = Form(""),
                            user: dict = Depends(get_current_user)):
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # Automatic mode is reserved for a future refund-API integration.
    if mode == "automatic":
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}?error=Automatic+refund+(API)+not+enabled+yet+-+use+Manual",
            status_code=302)

    # Record the refund in order_history only — order_status is NOT touched.
    ow_orders.log_partial_refund(order_id, amount=amount, reason=reason,
                                 mode="manual", changed_by=user["id"])

    channels = []
    if notify_email:
        channels.append("email")
    if notify_whatsapp:
        channels.append("whatsapp")
    if notify_sms:
        channels.append("sms")

    results = {}
    if channels:
        try:
            results = notifications.notify_ow_partial_refund(
                order, amount=amount, reason=reason, channels=channels) or {}
        except Exception as ne:
            logging.warning("Optiwar partial-refund notification failed for %s: %s", order_id, ne)

    # Log each notification channel outcome into order_history too, with the
    # provider receipt/reference (msg91 request_id, etc.) as proof.
    label = {"email": "Email", "whatsapp": "WhatsApp", "sms": "SMS"}
    for ch in channels:
        ok = False
        ref = ""
        res = results.get(ch)
        if isinstance(res, (tuple, list)) and res:
            ok = bool(res[0])
            if len(res) > 1:
                ref = notifications._extract_reference(str(res[1]))
        state = "sent" if ok else "failed"
        note = f"Partial refund notification {state} via {label.get(ch, ch)}"
        if ref:
            note += f" (ref {ref})"
        ow_orders.add_history(order_id, note)

    # Final step: notify the Accounts team so the refund gets booked. admin@optiwar.com
    # is always CC'd (handled inside the notifier). Includes the EWS receipts above.
    acct_to = (accounts_email or "").strip()
    if notify_accounts and acct_to:
        try:
            acct_res = notifications.notify_ow_refund_accounts(
                order, accounts_email=acct_to, amount=amount, reason=reason,
                channel_results=results, refunded_by=user.get("username") or user["id"])
            ok = False
            r = (acct_res or {}).get("accounts")
            if isinstance(r, (tuple, list)) and r:
                ok = bool(r[0])
            ow_orders.add_history(
                order_id,
                f"Refund summary {'sent' if ok else 'failed'} to accounts ({acct_to}) — CC admin@optiwar.com")
        except Exception as ae:
            logging.warning("Optiwar refund accounts email failed for %s: %s", order_id, ae)

    auth.log_activity(user["id"], "ow_partial_refund", "order", 0,
        {"order_id": order_id, "amount": amount, "reason": reason[:200],
         "channels": channels, "mode": "manual",
         "accounts_email": acct_to if notify_accounts else ""}, request.client.host)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{order_id}?msg=Partial+refund+recorded+(status+unchanged)",
        status_code=302)


# ═══════════════════════════════════════════
# REFUND THROUGH THE OPTIWAR API — prepare → review → confirm → track
# Optiwar holds the money and the truth; these routes draft, ask and watch.
# ═══════════════════════════════════════════
def _refund_facts(order_id: str):
    """The storefront's own numbers, plus the display strings the page needs."""
    facts = ow_refunds.preview(order_id)
    currency = facts.get("currency") or ""
    facts["captured_text"] = ow_refunds.money(facts.get("captured_minor"), currency)
    facts["already_refunded_text"] = ow_refunds.money(
        facts.get("already_refunded_minor"), currency)
    facts["max_refundable_text"] = ow_refunds.money(
        facts.get("max_refundable_minor"), currency)
    facts["max_refundable_input"] = "%d.%02d" % divmod(
        int(facts.get("max_refundable_minor") or 0), 100)
    # Fulfilment is per line in Optiwar; one dispatched line is enough for the
    # operator to be told the goods have already gone.
    facts["fulfilled"] = any(
        (l.get("fulfillment_status") or "") == "fulfilled"
        for l in facts.get("lines") or [])
    for r in facts.get("refunds") or []:
        r["amount_text"] = ow_refunds.money(r.get("amount_minor"), r.get("currency"))
        r["reason_label"] = ow_refunds.reason_label(r.get("reason_code"))
    return facts


@app.get("/orders/ow/{order_id}/refund/prepare", response_class=HTMLResponse)
async def ow_refund_prepare(request: Request, order_id: str,
                            error: str = Query(default=""),
                            amount: str = Query(default=""),
                            reason_code: str = Query(default=""),
                            refund_method: str = Query(default="gateway"),
                            comment: str = Query(default=""),
                            user: dict = Depends(get_current_user)):
    """Stage 1. Opens a draft. Nothing here can move money."""
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    facts = None
    api_error = ""
    try:
        facts = _refund_facts(order_id)
    except ow_refunds.RefundApiError as e:
        api_error = e.message
    draft = ""
    if facts and facts.get("max_refundable_minor"):
        draft = notifications._ai_refund_summary_fallback(
            order, ow_refunds.money(facts["max_refundable_minor"], facts["currency"]),
            ow_refunds.reason_label("CUSTOMER_REFUND"))
    return render("pages/ow_refund_prepare.html", request, user, order=order,
                  facts=facts, api_error=api_error or error,
                  reasons=ow_refunds.REASONS, draft_html=draft, seeded_draft=draft,
                  idempotency_key=ow_refunds.new_idempotency_key(order_id),
                  upi_today=datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat(),
                  api_configured=ow_refunds.configured(),
                  prefill_amount=amount, prefill_reason=reason_code,
                  prefill_method=refund_method, prefill_comment=comment)


@app.get("/orders/ow/{order_id}/refund/ai-draft")
async def ow_refund_ai_draft(request: Request, order_id: str,
                             amount: str = Query(default=""),
                             currency: str = Query(default=""),
                             reason_code: str = Query(default="CUSTOMER_REFUND"),
                             comment: str = Query(default=""),
                             user: dict = Depends(get_current_user)):
    """AI drafts words only, on request, and never blocks the refund: if DeepSeek
    is down the operator keeps the standard template already in the textarea."""
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    reason_label = ow_refunds.reason_label(reason_code)
    # The operator types digits; the customer must read a formatted amount, and AI
    # is handed that string rather than being asked to format money itself.
    amount_text = amount
    try:
        amount_text = ow_refunds.money(
            ow_refunds.parse_amount_minor(amount, currency), currency)
    except ow_refunds.RefundApiError:
        pass
    try:
        html = notifications._ai_refund_summary(
            order, amount_text, reason_label, (comment or "").strip())
        return JSONResponse({"ok": True, "html": html})
    except Exception as e:                       # never a 500 on a drafting failure
        logging.warning("Refund AI draft failed for %s: %s", order_id, e)
        return JSONResponse({"ok": False, "error": str(e),
                             "html": notifications._ai_refund_summary_fallback(
                                 order, amount_text, reason_label)})


@app.post("/orders/ow/{order_id}/refund/review", response_class=HTMLResponse)
async def ow_refund_review(request: Request, order_id: str,
                           amount: str = Form(""),
                           reason_code: str = Form("CUSTOMER_REFUND"),
                           refund_method: str = Form("gateway"),
                           upi_vpa: str = Form(""),
                           utr: str = Form(""),
                           paid_on: str = Form(""),
                           payee_name: str = Form(""),
                           comment: str = Form(""),
                           message_html: str = Form(""),
                           seeded_draft: str = Form(""),
                           idempotency_key: str = Form(""),
                           notify_email: str = Form(None),
                           notify_whatsapp: str = Form(None),
                           notify_sms: str = Form(None),
                           user: dict = Depends(get_current_user)):
    """Stage 2. Re-reads the storefront, shows exactly what the confirm button
    will do, and carries the same idempotency key forward."""
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    refund_method = (refund_method or "gateway").strip().lower()
    upi = {}
    try:
        if refund_method not in ("gateway", "upi"):
            raise ow_refunds.RefundApiError(
                "invalid_refund_method", "Choose Gateway or UPI")
        facts = _refund_facts(order_id)
        currency = facts.get("currency") or ""
        amount_minor = ow_refunds.parse_amount_minor(amount, currency)
        if amount_minor > int(facts.get("max_refundable_minor") or 0):
            raise ow_refunds.RefundApiError(
                "amount_exceeds_refundable",
                "Only %s is still refundable on this payment"
                % facts["max_refundable_text"])
        if refund_method == "upi":
            upi = ow_refunds.validate_upi(upi_vpa, utr, paid_on, payee_name)
    except ow_refunds.RefundApiError as e:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}/refund/prepare?error={quote(e.message)}",
            status_code=302)

    partial = amount_minor < int(facts.get("max_refundable_minor") or 0) or bool(
        facts.get("already_refunded_minor"))
    channels = [c for c, on in (("email", notify_email), ("whatsapp", notify_whatsapp),
                                ("sms", notify_sms)) if on]
    amount_text = ow_refunds.money(amount_minor, facts["currency"])
    reason_label = ow_refunds.reason_label(reason_code)

    # The draft was written for the amount the page was opened with. If the
    # operator changed the amount and left the standard wording alone, rewrite it
    # rather than promise the customer money that is not being sent.
    if message_html.strip() == seeded_draft.strip():
        message_html = notifications._ai_refund_summary_fallback(
            order, amount_text, reason_label, upi=upi or None)
    mismatch = ow_refunds.message_contradicts_amount(
        message_html, amount_minor, facts["currency"])

    return render("pages/ow_refund_review.html", request, user, order=order,
                  facts=facts, amount_minor=amount_minor,
                  amount_text=amount_text,
                  reason_code=reason_code, reason_label=reason_label,
                  message_mismatch=mismatch,
                  comment=comment, message_html=message_html,
                  requested_status=("Partially Refunded" if partial else "Refunded"),
                  channels=channels,
                  refund_method=refund_method, upi=upi,
                  idempotency_key=idempotency_key or ow_refunds.new_idempotency_key(order_id))



@app.post("/orders/ow/{order_id}/refund/dry-run", response_class=HTMLResponse)
async def ow_refund_dry_run(request: Request, order_id: str,
                            amount_minor: int = Form(...),
                            currency: str = Form(...),
                            reason_code: str = Form("CUSTOMER_REFUND"),
                            refund_method: str = Form("gateway"),
                            upi_vpa: str = Form(""),
                            utr: str = Form(""),
                            paid_on: str = Form(""),
                            payee_name: str = Form(""),
                            comment: str = Form(""),
                            message_html: str = Form(""),
                            requested_status: str = Form(""),
                            idempotency_key: str = Form(...),
                            channels: str = Form(""),
                            send_copy: str = Form(None),
                            user: dict = Depends(get_current_user)):
    """Everything the confirm button would do, shown instead of done: the exact
    request Optiwar would receive, and the exact email/WhatsApp/SMS the customer
    would get. Reads the storefront; writes nothing, sends nothing to nobody but
    the operator who asks for a copy."""
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    chans = [c for c in (channels or "").split(",") if c.strip()]
    operator = user.get("username") or user.get("email") or str(user["id"])
    amount_text = ow_refunds.money(amount_minor, currency)
    reason_label = ow_refunds.reason_label(reason_code)
    refund_method = (refund_method or "gateway").strip().lower()
    if refund_method not in ("gateway", "upi"):
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}/refund/prepare?error="
            f"{quote('Choose Gateway or UPI')}", status_code=302)
    upi = {}
    if refund_method == "upi":
        try:
            upi = ow_refunds.validate_upi(upi_vpa, utr, paid_on, payee_name)
        except ow_refunds.RefundApiError as e:
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}/refund/prepare?error={quote(e.message)}",
                status_code=302)

    api_error = ""
    facts = {}
    try:
        facts = _refund_facts(order_id)
    except ow_refunds.RefundApiError as e:
        api_error = e.message

    preview = notifications.notify_ow_refund_issued(
        order, amount_text=amount_text, reason_label=reason_label,
        note=comment, channels=chans, body_html=message_html,
        partial=(requested_status == "Partially Refunded"), dry_run=True,
        upi=upi if refund_method == "upi" else None)

    checks = []
    if facts:
        remaining = int(facts.get("max_refundable_minor") or 0)
        checks = [
            ("Payment found on Optiwar", bool(facts.get("payment_ref")),
             facts.get("payment_ref") or "no payment reference"),
            ("Currency matches the captured payment",
             (currency or "").upper() == (facts.get("currency") or "").upper(),
             "requesting %s against %s captured" % (currency, facts.get("currency"))),
            ("Amount is within the refundable balance", 0 < amount_minor <= remaining,
             "%s of %s" % (amount_text, facts.get("max_refundable_text"))),
            ("Customer message names the right amount",
             not ow_refunds.message_contradicts_amount(message_html, amount_minor,
                                                       currency),
             "the message must not promise a different figure"),
        ]

    copy_result = None
    if send_copy:
        to = (user.get("email") or "").strip()
        if to:
            copy_result = notifications.send_email(
                to, "[DRY RUN — nothing refunded] " + preview["subject"],
                preview["html"], preview["text"])
        else:
            copy_result = (False, "Your ops account has no email address")

    auth.log_activity(user["id"], "ow_refund_dry_run", "order", 0,
        {"order_id": order_id, "amount_minor": amount_minor, "currency": currency,
         "idempotency_key": idempotency_key, "channels": chans},
        request.client.host)

    if refund_method == "upi":
        request_body = {
            "amount_minor": amount_minor, "currency": currency,
            "reason_code": reason_code, "comment": comment,
            "requested_status": requested_status or None,
            "approved_message": message_html,
        }
        request_body.update(upi)
        api_url = "%s/api/ops/orders/%s/refund/upi" % (
            ow_refunds.base_url(), order_id)
    else:
        request_body = {
            "amount_minor": amount_minor, "currency": currency,
            "reason_code": reason_code, "comment": comment,
            "requested_status": requested_status or None,
            "notify": {"email": "email" in chans, "sms": "sms" in chans,
                       "whatsapp": "whatsapp" in chans},
            "approved_message": message_html,
        }
        api_url = "%s/api/ops/orders/%s/refund" % (
            ow_refunds.base_url(), order_id)

    return render("pages/ow_refund_dry_run.html", request, user, order=order,
                  facts=facts, api_error=api_error, preview=preview,
                  checks=checks, channels=chans, amount_minor=amount_minor,
                  amount_text=amount_text, currency=currency,
                  reason_code=reason_code, reason_label=reason_label,
                  comment=comment, message_html=message_html,
                  requested_status=requested_status,
                  idempotency_key=idempotency_key, operator=operator,
                  refund_method=refund_method, upi=upi,
                  copy_result=copy_result,
                  api_url=api_url,
                  request_json=json.dumps(request_body, indent=2))


@app.post("/orders/ow/{order_id}/refund/confirm")
async def ow_refund_confirm(request: Request, order_id: str,
                            amount_minor: int = Form(...),
                            currency: str = Form(...),
                            reason_code: str = Form("CUSTOMER_REFUND"),
                            refund_method: str = Form("gateway"),
                            upi_vpa: str = Form(""),
                            utr: str = Form(""),
                            paid_on: str = Form(""),
                            payee_name: str = Form(""),
                            comment: str = Form(""),
                            message_html: str = Form(""),
                            requested_status: str = Form(""),
                            idempotency_key: str = Form(...),
                            channels: str = Form(""),
                            message_ack: str = Form(None),
                            user: dict = Depends(get_current_user)):
    """Issue a Gateway refund or record a staff-paid UPI refund."""
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    status_before = order.get("status_name")
    refund_method = (refund_method or "gateway").strip().lower()
    upi = {}
    if refund_method not in ("gateway", "upi"):
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}/refund/prepare?error="
            f"{quote('Choose Gateway or UPI')}", status_code=302)
    if refund_method == "upi":
        try:
            upi = ow_refunds.validate_upi(upi_vpa, utr, paid_on, payee_name)
        except ow_refunds.RefundApiError as e:
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}/refund/prepare?error={quote(e.message)}",
                status_code=302)
    if not message_ack and ow_refunds.message_contradicts_amount(
            message_html, amount_minor, currency):
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}/refund/prepare?error="
            + quote("The customer message names a different amount than the %s "
                    "refund. Fix the message, or tick the acknowledgement on the "
                    "preview screen." % ow_refunds.money(amount_minor, currency)),
            status_code=302)
    chans = [c for c in (channels or "").split(",") if c.strip()]
    operator = user.get("username") or user.get("email") or str(user["id"])

    auth.log_activity(user["id"], "ow_refund_requested", "order", 0,
        {"order_id": order_id, "amount_minor": amount_minor, "currency": currency,
         "reason_code": reason_code, "idempotency_key": idempotency_key,
         "requested_status": requested_status, "channels": chans,
         "refund_method": refund_method},
        request.client.host)

    try:
        if refund_method == "upi":
            refund = ow_refunds.record_upi(
                order_id, amount_minor=amount_minor, currency=currency,
                reason_code=reason_code, comment=comment,
                idempotency_key=idempotency_key, operator=operator,
                approved_message=message_html,
                requested_status=requested_status or None, **upi)
        else:
            refund = ow_refunds.execute(
                order_id, amount_minor=amount_minor, currency=currency,
                reason_code=reason_code, comment=comment,
                idempotency_key=idempotency_key, operator=operator,
                approved_message=message_html, requested_status=requested_status or None,
                notify={"email": "email" in chans, "sms": "sms" in chans,
                        "whatsapp": "whatsapp" in chans})
    except ow_refunds.RefundApiError as e:
        auth.log_activity(user["id"], "ow_refund_failed", "order", 0,
            {"order_id": order_id, "error": e.code, "status": e.status,
             "idempotency_key": idempotency_key}, request.client.host)
        if e.code == "api_unreachable":
            # It may have gone through. Send the operator to tracking, not to retry.
            return RedirectResponse(
                f"{ROOT_PATH}/orders/ow/{order_id}/refund/track"
                f"?key={quote(idempotency_key, safe='')}&error={quote(e.message)}",
                status_code=302)
        return RedirectResponse(
            f"{ROOT_PATH}/orders/ow/{order_id}/refund/prepare?error={quote(e.message)}",
            status_code=302)

    amount_text = ow_refunds.money(refund.get("amount_minor"), refund.get("currency"))
    activity = {
        "order_id": order_id, "amount_minor": refund.get("amount_minor"),
        "currency": refund.get("currency"), "status": refund.get("status"),
        "provider_refund_id": refund.get("provider_refund_id"),
        "idempotency_key": idempotency_key,
    }
    if refund_method == "upi":
        activity.update({"upi_vpa": upi["upi_vpa"], "utr": upi["utr"]})
    auth.log_activity(
        user["id"], "ow_refund_upi_recorded" if refund_method == "upi"
        else "ow_refund_executed", "order", 0, activity, request.client.host)

    # Notifications come after the selected refund method completes, and a
    # failure here is reported as a failed notification — never as a failed refund.
    # A replay is the same refund answered twice; the customer heard about it the
    # first time and must not be told again.
    channel_results = {}
    successful_statuses = ("PROCESSED",) if refund_method == "upi" else (
        "PROCESSED", "PENDING")
    if (chans and not refund.get("replayed")
            and refund.get("status") in successful_statuses):
        try:
            channel_results = notifications.notify_ow_refund_issued(
                order, amount_text=amount_text,
                reason_label=ow_refunds.reason_label(reason_code),
                note=comment, channels=chans, body_html=message_html,
                partial=(refund.get("refund_type") == "PARTIAL"),
                upi=upi if refund_method == "upi" else None) or {}
        except Exception as ne:
            logging.warning("Refund notification failed for %s: %s", order_id, ne)

    if (refund_method == "upi" and not refund.get("replayed")
            and refund.get("status") == "PROCESSED"):
        accounts_reason = (
            "%s — paid by UPI to %s (%s), UTR %s, on %s"
            % (ow_refunds.reason_label(reason_code), upi["upi_vpa"],
               upi["payee_name"], upi["utr"], upi["paid_on"]))
        try:
            notifications.notify_ow_refund_accounts(
                order, accounts_email=config.OPTIWAR_ACCOUNTS_EMAIL,
                amount=amount_text, reason=accounts_reason,
                channel_results=channel_results, refunded_by=operator,
                method="UPI", status_before=status_before,
                status_after=requested_status or status_before)
        except Exception as ae:
            logging.warning("Optiwar UPI refund accounts email failed for %s: %s",
                            order_id, ae)

    success_message = ("UPI refund %s recorded" % amount_text
                       if refund_method == "upi"
                       else "Refund %s sent to the provider" % amount_text)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/ow/{order_id}/refund/track"
        f"?key={quote(idempotency_key, safe='')}"
        f"&msg={quote(success_message)}",
        status_code=302)


@app.get("/orders/ow/{order_id}/refund/track", response_class=HTMLResponse)
async def ow_refund_track(request: Request, order_id: str, key: str = "",
                          user: dict = Depends(get_current_user)):
    """Live state of one refund, straight from Optiwar's ledger and (while it is
    still pending) from the payment provider.

    The key travels as a query parameter because it contains slashes, and the
    Apache in front of this app refuses encoded slashes in a path."""
    idempotency_key = key
    order = ow_orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    refund = None
    api_error = ""
    try:
        refund = ow_refunds.track(idempotency_key)
        refund["amount_text"] = ow_refunds.money(refund.get("amount_minor"),
                                                 refund.get("currency"))
        refund["reason_label"] = ow_refunds.reason_label(refund.get("reason_code"))
        refund["upi_fallback"] = (
            frame_offers_svc.upi_fallback_state(
                refund.get("error_text"), refund.get("payment_ref"), rzp._call)
            if refund.get("status") == "FAILED" else None)
    except ow_refunds.RefundApiError as e:
        api_error = e.message
    notif_logs = [n for n in notifications.get_notification_logs(order_id)
                  if (n.get("event") or "").startswith("ow_refund")]
    return render("pages/ow_refund_track.html", request, user, order=order,
                  refund=refund, api_error=api_error,
                  idempotency_key=idempotency_key, notif_logs=notif_logs)


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(request: Request, order_id: int, user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    all_statuses = orders.get_order_statuses()
    supplier_list = orders.get_supplier_list()
    irn_info = einvoice.get_irn_status_summary(order_id)
    pending_refund = order_edits.get_pending_refund(order_id)
    edit_history = order_edits.get_order_edits(order_id)
    notif_logs = notifications.get_notification_logs(order_id)
    eur_rate = float(order.get("currency_value") or 0)
    if eur_rate <= 0:
        eur_rate = float(order.get("eur_rate") or 0.0108)
    return render("pages/order_detail.html", request, user, order=order,
                  all_statuses=all_statuses, supplier_list=supplier_list,
                  irn_info=irn_info, pending_refund=pending_refund,
                  edit_history=edit_history, notif_logs=notif_logs,
                  eur_rate=eur_rate)


@app.post("/orders/{order_id}/supplier")
async def assign_supplier(request: Request, order_id: int,
                          supplier_name: str = Form(...),
                          supplier_reference: str = Form(""),
                          expected_ship_date: str = Form(""),
                          user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    # Build product list text for the supplier_order record
    product_lines = []
    for p in order.get("products", []):
        line = f"{p['name']} (x{p['quantity']})"
        if p.get("has_eye_options"):
            for opt in p.get("left_eye", []):
                line += f"\n  L: {opt['name']}: {opt['value']}"
            for opt in p.get("right_eye", []):
                line += f"\n  R: {opt['name']}: {opt['value']}"
        product_lines.append(line)
    product_list = "\n".join(product_lines)
    old_supplier = order.get("supplier")
    old_name = old_supplier["supplier_name"] if old_supplier else "(none)"
    orders.assign_supplier(order_id, supplier_name, product_list, supplier_reference,
                           expected_ship_date)
    auth.log_activity(user["id"], "supplier_assigned", "order", order_id,
        {"old_supplier": old_name, "new_supplier": supplier_name,
         "reference": supplier_reference, "expected_ship_date": expected_ship_date},
        request.client.host)
    return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}?msg=Supplier+assigned", status_code=302)


@app.post("/suppliers/add")
async def add_supplier_route(request: Request,
                             name: str = Form(...),
                             email: str = Form(""),
                             address: str = Form(""),
                             phone: str = Form(""),
                             order_id: str = Form(""),
                             user: dict = Depends(get_current_user)):
    new_id = orders.add_supplier(name, email, address, phone)
    if new_id:
        auth.log_activity(user["id"], "supplier_added", "supplier_list", new_id,
            {"name": name}, request.client.host)
        msg = "Supplier+added"
    else:
        msg = "Supplier+exists+or+invalid"
    if order_id:
        return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}?msg={msg}", status_code=302)
    return RedirectResponse(f"{ROOT_PATH}/orders?msg={msg}", status_code=302)


# ═══════════════════════════════════════════
# ORDER EDITING / PARTIAL SHIPMENT
# ═══════════════════════════════════════════
@app.get("/orders/{order_id}/edit", response_class=HTMLResponse)
async def edit_order_page(request: Request, order_id: int, user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    order_eur_rate = float(order.get("currency_value") or 0)
    if order_eur_rate <= 0:
        order_eur_rate = float(order.get("eur_rate") or 0.0108)
    return render("pages/edit_order.html", request, user, order=order, eur_rate=order_eur_rate)


@app.get("/orders/{order_id}/edit-rx", response_class=HTMLResponse)
async def edit_rx_page(request: Request, order_id: int, user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    order_eur_rate = float(order.get("currency_value") or 0)
    if order_eur_rate <= 0:
        order_eur_rate = float(order.get("eur_rate") or 0.0108)
    return render("pages/edit_rx.html", request, user, order=order, eur_rate=order_eur_rate)


@app.post("/orders/{order_id}/edit-rx/preview")
async def edit_rx_preview(request: Request, order_id: int,
                         user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    form = await request.form()
    notes = form.get("notes", "")
    ticket_ref = form.get("ticket_ref", "")
    # Collect prescription changes
    rx_updates = {}
    for key, val in form.items():
        if key.startswith("rx_"):
            opt_id = key.replace("rx_", "")
            rx_updates[opt_id] = val
    # Collect qty changes (optional on rx page)
    new_quantities = {}
    for key, val in form.items():
        if key.startswith("qty_"):
            opt_id = key.replace("qty_", "")
            new_quantities[opt_id] = val
    try:
        refund_data = order_edits.calculate_refund(order_id, new_quantities)
    except ValueError as e:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/edit-rx?error={str(e)}", status_code=302)
    rx_changes = order_edits.calculate_rx_changes(order_id, rx_updates) if rx_updates else []
    if not refund_data["changes"] and not rx_changes:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/edit-rx?error=No+changes+detected", status_code=302)
    email_draft = order_edits.build_rx_email_with_ai(
        order, rx_changes, refund_data["changes"],
        refund_data["total_refund_eur"], ticket_ref)
    return render("pages/edit_preview.html", request, user, order=order,
                  refund_data=refund_data, email_draft=email_draft,
                  refund_data_json=json.dumps(refund_data),
                  rx_changes=rx_changes,
                  rx_changes_json=json.dumps(rx_changes),
                  notes=notes, ticket_ref=ticket_ref)





# ─── Product Search API (for autocomplete) ───

@app.get("/api/products/search")
async def api_product_search(q: str = "", user: dict = Depends(get_current_user)):
    """Search product catalog for autocomplete. Returns product_id, model, eur_price."""
    from database import db_cursor
    if not q or len(q) < 2:
        return []
    with db_cursor() as cur:
        cur.execute("""
            SELECT p.product_id, p.model,
                   ROUND(COALESCE(ps.price, p.price) * c.value, 2) AS eur_price
            FROM product p
            LEFT JOIN product_special ps ON p.product_id = ps.product_id
            CROSS JOIN currency c
            WHERE c.code = 'EUR'
              AND p.status = 1
              AND LOWER(p.model) LIKE LOWER(%s)
            ORDER BY p.model
            LIMIT 15
        """, (f"%{q}%",))
        rows = cur.fetchall()
    results = []
    for r in rows:
        results.append({
            "product_id": r["product_id"],
            "model": r["model"],
            "eur_price": float(r["eur_price"]) if r["eur_price"] else 0.0
        })
    return results

# ─── Edit Product Change ───

@app.get("/orders/{order_id}/edit-product", response_class=HTMLResponse)
async def edit_product_change_page(request: Request, order_id: int, user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    order_eur_rate = float(order.get("currency_value") or 0)
    if order_eur_rate <= 0:
        order_eur_rate = float(order.get("eur_rate") or 0.0108)
    return render("pages/edit_product_change.html", request, user, order=order, eur_rate=order_eur_rate)


@app.post("/orders/{order_id}/edit-product/preview")
async def edit_product_change_preview(request: Request, order_id: int,
                                      user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    form = await request.form()
    notes = form.get("notes", "")
    ticket_ref = form.get("ticket_ref", "")
    order_eur_rate = float(order.get("currency_value") or 0)
    if order_eur_rate <= 0:
        order_eur_rate = float(order.get("eur_rate") or 0.0108)
    product_changes = []
    total_price_diff = 0.0
    for p in order.get("products", []):
        pid = p["order_product_id"]
        new_name = form.get(f"new_name_{pid}", "").strip()
        new_product_id = form.get(f"new_product_id_{pid}", "").strip()
        old_name = form.get(f"old_name_{pid}", "").strip()
        new_price_eur = form.get(f"new_price_{pid}", "")
        old_price_eur = form.get(f"old_price_eur_{pid}", "")
        quantity = int(form.get(f"quantity_{pid}", "1") or "1")
        if not new_price_eur:
            new_price_eur = str(round(float(p["price"]) * order_eur_rate, 2))
        if not old_price_eur:
            old_price_eur = str(round(float(p["price"]) * order_eur_rate, 2))
        name_changed = new_name and old_name and new_name != old_name
        price_changed = abs(float(new_price_eur) - float(old_price_eur)) > 0.001
        if name_changed or price_changed:
            rx_parts = []
            for eye_label, eye_key in [("L", "left_eye"), ("R", "right_eye")]:
                eye_opts = p.get(eye_key, [])
                for opt in eye_opts:
                    if opt["name"].strip().lower() != "quantity":
                        rx_parts.append(f"{eye_label}-" + opt["name"] + ": " + opt["value"])
            rx_summary = ", ".join(rx_parts) if rx_parts else "No prescription data"
            price_diff = (float(new_price_eur) - float(old_price_eur)) * quantity
            total_price_diff += price_diff
            product_changes.append({
                "order_product_id": pid,
                "old_name": old_name,
                "new_name": new_name if new_name else old_name,
                "new_product_id": new_product_id,
                "rx_summary": rx_summary,
                "old_price_eur": float(old_price_eur),
                "new_price_eur": float(new_price_eur),
                "quantity": quantity,
                "price_diff_eur": round(price_diff, 2),
            })
    if not product_changes:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/edit-product?error=No+changes+detected", status_code=302)
    email_draft = order_edits.build_product_change_email_with_ai(
        order, product_changes, ticket_ref, round(total_price_diff, 2))
    return render("pages/edit_product_change_preview.html", request, user,
                  order=order, email_draft=email_draft,
                  product_changes=product_changes,
                  product_changes_json=json.dumps(product_changes),
                  price_difference=round(total_price_diff, 2),
                  notes=notes, ticket_ref=ticket_ref)

@app.post("/orders/{order_id}/edit-product/confirm")
async def edit_product_change_confirm(request: Request, order_id: int,
                                      user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    form = await request.form()
    product_changes = json.loads(form.get("product_changes_json", "[]"))
    notes = form.get("notes", "")
    ticket_ref = form.get("ticket_ref", "")
    send_email = form.get("send_email") == "1"
    cc_emails_raw = form.get("cc_emails", "")
    cc_list = [e.strip() for e in cc_emails_raw.split(",") if e.strip() and "@" in e.strip()]

    if not product_changes:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=No+changes+to+apply", status_code=302)

    # Apply product name changes
    order_edits.save_product_change(order_id, product_changes, user["id"], notes)

    email_sent = False
    if send_email:
        email_body_html = form.get("email_body_html", "")
        email_subject = form.get("email_subject", "")
        email_to = form.get("email_to", "")
        email_bcc = form.get("email_bcc", "admin@lensbazaar.com")
        if email_body_html and email_subject and email_to:
            email_draft = {
                "to": email_to,
                "bcc": email_bcc,
                "subject": email_subject,
                "body_html": email_body_html,
            }
        else:
            email_draft = order_edits.build_product_change_email_with_ai(
                order, product_changes, ticket_ref)
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = config.SMTP_FROM or config.SMTP_USER
            msg["To"] = email_draft["to"]
            msg["Subject"] = email_draft["subject"]
            msg.attach(MIMEText(email_draft["body_html"], "html"))
            bcc = email_draft.get("bcc", "admin@lensbazaar.com")
            if cc_list:
                msg["Cc"] = ", ".join(cc_list)
            recipients = [email_draft["to"], bcc] + cc_list
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(config.SMTP_USER, config.SMTP_PASS)
                server.sendmail(config.SMTP_FROM or config.SMTP_USER,
                                recipients, msg.as_string())
            email_sent = True
            auth.log_activity(user["id"], "product_change_email_sent", "order", order_id,
                {"to": email_draft["to"]}, request.client.host)
            notifications._save_notification_log(
                order_id, "email", "product_changed", email_draft["to"],
                True, "Email sent", user["username"])
        except Exception as e:
            log.error("Failed to send product change email for order %s: %s", order_id, e)
            notifications._save_notification_log(
                order_id, "email", "product_changed", email_draft.get("to", ""),
                False, str(e), user["username"])

    auth.log_activity(user["id"], "product_change", "order", order_id,
        {"changes": product_changes, "email_sent": email_sent}, request.client.host)

    status_msg = "Product+changed+successfully"
    if email_sent:
        status_msg += "+and+email+sent"
    return RedirectResponse(
        f"{ROOT_PATH}/orders/{order_id}?success={status_msg}", status_code=302)

@app.post("/orders/{order_id}/edit/preview")
async def edit_order_preview(request: Request, order_id: int,
                            user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    form = await request.form()
    notes = form.get("notes", "")
    ticket_ref = form.get("ticket_ref", "")

    # Collect quantity changes
    new_quantities = {}
    for key, val in form.items():
        if key.startswith("qty_"):
            opt_id = key.replace("qty_", "")
            new_quantities[opt_id] = val

    # Collect prescription changes
    rx_updates = {}
    for key, val in form.items():
        if key.startswith("rx_"):
            opt_id = key.replace("rx_", "")
            rx_updates[opt_id] = val

    # Calculate quantity refund
    try:
        refund_data = order_edits.calculate_refund(order_id, new_quantities)
    except ValueError as e:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/edit?error={str(e)}", status_code=302)

    # Calculate prescription changes
    rx_changes = order_edits.calculate_rx_changes(order_id, rx_updates) if rx_updates else []

    if not refund_data["changes"] and not rx_changes:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/edit?error=No+changes+detected", status_code=302)

    # Use AI to draft email if there are prescription changes, otherwise use standard template
    if rx_changes:
        email_draft = order_edits.build_rx_email_with_ai(
            order, rx_changes, refund_data["changes"],
            refund_data["total_refund_eur"], ticket_ref)
    else:
        email_draft = order_edits.build_customer_email(
            order, refund_data["changes"], refund_data["total_refund_eur"])

    return render("pages/edit_preview.html", request, user, order=order,
                  refund_data=refund_data, email_draft=email_draft,
                  refund_data_json=json.dumps(refund_data),
                  rx_changes=rx_changes,
                  rx_changes_json=json.dumps(rx_changes),
                  notes=notes, ticket_ref=ticket_ref)


@app.post("/orders/{order_id}/edit/confirm")
async def edit_order_confirm(request: Request, order_id: int,
                            user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    form = await request.form()
    refund_data = json.loads(form.get("refund_data_json", "{}"))
    rx_changes = json.loads(form.get("rx_changes_json", "[]"))
    notes = form.get("notes", "")
    ticket_ref = form.get("ticket_ref", "")
    send_email = form.get("send_email") == "1"
    cc_emails_raw = form.get("cc_emails", "")
    cc_list = [e.strip() for e in cc_emails_raw.split(",") if e.strip() and "@" in e.strip()]
    if not refund_data.get("changes") and not rx_changes:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=No+changes+to+apply", status_code=302)

    # Apply prescription changes first
    if rx_changes:
        order_edits.save_rx_changes(rx_changes)
    if refund_data.get("changes"):
        edit_id = order_edits.save_order_edit(
            order_id, refund_data["changes"],
            refund_data["total_refund_eur"], refund_data["total_refund_inr"],
            refund_data.get("razorpay_payment_id", ""),
            user["id"], notes)
    else:
        edit_id = None
    email_sent = False
    if send_email:
        # Use the agent-edited email content from the form (editable draft)
        email_body_html = form.get("email_body_html", "")
        email_subject = form.get("email_subject", "")
        email_to = form.get("email_to", "")
        email_bcc = form.get("email_bcc", "admin@lensbazaar.com")
        if email_body_html and email_subject and email_to:
            email_draft = {
                "to": email_to,
                "bcc": email_bcc,
                "subject": email_subject,
                "body_html": email_body_html,
            }
        elif rx_changes:
            email_draft = order_edits.build_rx_email_with_ai(
                order, rx_changes, refund_data.get("changes", []),
                refund_data.get("total_refund_eur", 0), ticket_ref)
        else:
            email_draft = order_edits.build_customer_email(
                order, refund_data["changes"], refund_data["total_refund_eur"])
        import smtplib
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = config.SMTP_FROM or config.SMTP_USER
            msg["To"] = email_draft["to"]
            msg["Subject"] = email_draft["subject"]
            msg.attach(MIMEText(email_draft["body_html"], "html"))
            bcc = email_draft.get("bcc", "admin@lensbazaar.com")
            if cc_list:
                msg["Cc"] = ", ".join(cc_list)
            recipients = [email_draft["to"], bcc] + cc_list
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
                server.starttls()
                server.login(config.SMTP_USER, config.SMTP_PASS)
                server.sendmail(config.SMTP_FROM or config.SMTP_USER,
                                recipients, msg.as_string())
            email_sent = True
            auth.log_activity(user["id"], "edit_email_sent", "order", order_id,
                {"to": email_draft["to"]}, request.client.host)
            notifications._save_notification_log(
                order_id, "email", "order_edited", email_draft["to"],
                True, "Email sent", user["username"])
        except Exception as e:
            logging.error("Failed to send edit email for order %s: %s", order_id, e)
            notifications._save_notification_log(
                order_id, "email", "order_edited", email_draft.get("to", ""),
                False, str(e), user["username"])
    # Build status message
    status_parts = []
    if refund_data.get("changes"):
        status_parts.append(f"{len(refund_data['changes'])} qty adjustment(s), refund EUR {refund_data['total_refund_eur']:.2f}")
    if rx_changes:
        status_parts.append(f"{len(rx_changes)} prescription change(s)")
    if ticket_ref:
        status_parts.append(f"Ticket: {ticket_ref}")
    status_msg = "Order edited: " + "; ".join(status_parts) if status_parts else "Order edited"

    orders.update_order_status(order_id, order["order_status_id"],
        status_msg, 1 if email_sent else 0, added_by=user["username"])
    auth.log_activity(user["id"], "order_edited", "order", order_id,
        {"edit_id": edit_id, "refund_eur": refund_data.get("total_refund_eur", 0),
         "qty_changes": len(refund_data.get("changes", [])),
         "rx_changes": len(rx_changes), "ticket_ref": ticket_ref},
        request.client.host)
    msg = "Order+edited."
    if refund_data.get("total_refund_eur", 0) > 0:
        msg += f"+Refund+pending:+EUR+{refund_data['total_refund_eur']:.2f}"
    if rx_changes:
        msg += f"+{len(rx_changes)}+prescription+change(s)+applied"
    return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}?msg={msg}", status_code=302)


@app.post("/orders/{order_id}/refund-complete")
async def mark_refund_complete(request: Request, order_id: int,
                              edit_id: int = Form(...),
                              refund_reference: str = Form(""),
                              user: dict = Depends(get_current_user)):
    order_edits.mark_refund_completed(edit_id, refund_reference[:200])
    auth.log_activity(user["id"], "refund_completed", "order", order_id,
        {"edit_id": edit_id, "reference": refund_reference}, request.client.host)
    orders.update_order_status(order_id, 22,
        f"Partial refund completed. Ref: {refund_reference}",
        0, added_by=user["username"])
    return RedirectResponse(
        f"{ROOT_PATH}/orders/{order_id}?msg=Refund+marked+as+completed", status_code=302)


@app.get("/orders/{order_id}/refund-preview", response_class=HTMLResponse)
async def eu_refund_preview(request: Request, order_id: int,
                            refund_type: str = Query(default="full"),
                            rate: float = Query(default=0.0),
                            deduction_return: float = Query(default=0.0),
                            deduction_freight: float = Query(default=0.0),
                            reason: str = Query(default=""),
                            channels: str = Query(default="email,whatsapp,sms"),
                            email_body: str = Query(default=""),
                            user: dict = Depends(get_current_user)):
    """Dry-run: shows exactly what the customer would receive. Nothing is sent.
    If ``email_body`` is supplied (operator-edited AI draft) it is used verbatim as the
    narrative; otherwise a fresh DeepSeek draft is generated."""
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    calc = order_edits.compute_refund_amounts(
        order, refund_type=refund_type, rate=rate,
        deduction_return_inr=deduction_return, deduction_freight_inr=deduction_freight)
    if email_body.strip():
        email = order_edits.render_refund_email_from_body(order, calc, email_body, reason=reason)
    else:
        email = order_edits.build_refund_email_with_ai(order, calc, reason=reason)
    admin_email = order_edits.build_refund_admin_email(
        order, calc, reason=reason, refunded_by=user.get("username", ""))
    chans = [c for c in channels.split(",") if c.strip()] or ["email"]
    preview = notifications.notify_order_refund(
        order, refund_eur=calc["refund_eur"], refund_inr=calc["refund_inr"],
        reason=reason, refund_type=calc["refund_type"], channels=chans,
        email_subject=email["subject"], email_html=email["body_html"],
        email_text=email["body_text"], admin_subject=admin_email["subject"],
        admin_html=admin_email["body_html"], admin_text=admin_email["body_text"],
        dry_run=True)
    return render("pages/refund_dry_run.html", request, user, order=order,
                  preview=preview, calc=calc, email_inner=email.get("body_inner", ""),
                  channels=chans, reason=reason, title="Refund — Dry Run")


@app.post("/orders/{order_id}/refund")
async def eu_refund(request: Request, order_id: int,
                    refund_type: str = Form("full"),
                    rate: float = Form(0.0),
                    deduction_return: float = Form(0.0),
                    deduction_freight: float = Form(0.0),
                    reason: str = Form(""),
                    notify_email: str = Form(None),
                    notify_whatsapp: str = Form(None),
                    notify_sms: str = Form(None),
                    email_body: str = Form(""),
                    user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    calc = order_edits.compute_refund_amounts(
        order, refund_type=refund_type, rate=rate,
        deduction_return_inr=deduction_return, deduction_freight_inr=deduction_freight)

    # Record the refund (pending until the operator books it in Razorpay) and set status.
    edit_id = order_edits.save_refund(order_id, calc, reason.strip(), user["id"])
    status_id = 11 if calc["refund_type"] == "full" else 22
    comment = (f"{'Full' if status_id == 11 else 'Partial'} refund recorded: "
               f"€{calc['refund_eur']:.2f} (₹{calc['refund_inr']:.2f})"
               + (f" — {reason.strip()}" if reason.strip() else ""))
    orders.update_order_status(order_id, status_id, comment, 1, added_by=user["username"])

    channels = []
    if notify_email:
        channels.append("email")
    if notify_whatsapp:
        channels.append("whatsapp")
    if notify_sms:
        channels.append("sms")

    results = {}
    if channels:
        try:
            if email_body.strip():
                email = order_edits.render_refund_email_from_body(
                    order, calc, email_body, reason=reason.strip())
            else:
                email = order_edits.build_refund_email_with_ai(order, calc, reason=reason.strip())
            admin_email = order_edits.build_refund_admin_email(
                order, calc, reason=reason.strip(), refunded_by=user.get("username", ""))
            results = notifications.notify_order_refund(
                order, refund_eur=calc["refund_eur"], refund_inr=calc["refund_inr"],
                reason=reason.strip(), refund_type=calc["refund_type"], channels=channels,
                email_subject=email["subject"], email_html=email["body_html"],
                email_text=email["body_text"], admin_subject=admin_email["subject"],
                admin_html=admin_email["body_html"], admin_text=admin_email["body_text"]) or {}
        except Exception as ne:
            logging.warning("EU refund notification failed for %s: %s", order_id, ne)

    # Log each channel outcome (with provider receipt) into order history as proof.
    label = {"email": "Email", "whatsapp": "WhatsApp", "sms": "SMS"}
    for ch in channels:
        ok, ref = False, ""
        res = results.get(ch)
        if isinstance(res, (tuple, list)) and res:
            ok = bool(res[0])
            if len(res) > 1:
                ref = notifications._extract_reference(str(res[1]))
        note = f"Refund notification {'sent' if ok else 'failed'} via {label.get(ch, ch)}"
        if ref:
            note += f" (ref {ref})"
        orders.update_order_status(order_id, status_id, note,
                                   1 if ok else 0, added_by=user["username"])

    auth.log_activity(user["id"], "eu_refund", "order", order_id,
        {"refund_type": calc["refund_type"], "refund_eur": calc["refund_eur"],
         "refund_inr": calc["refund_inr"], "rate": calc["rate"],
         "deduction_return": calc["deduction_return_inr"],
         "deduction_freight": calc["deduction_freight_inr"],
         "channels": channels, "edit_id": edit_id}, request.client.host)
    return RedirectResponse(
        f"{ROOT_PATH}/orders/{order_id}?msg=Refund+recorded+(enter+Razorpay+ref+to+complete)",
        status_code=302)


@app.get("/orders/{order_id}/invoice", response_class=HTMLResponse)
async def order_invoice(request: Request, order_id: int, user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    invoice = invoice_dates.get_eu_invoice(order_id)
    if not invoice:
        return _invoice_issue_prompt(request, f"E-{order_id}", order,
                                     f"{ROOT_PATH}/orders/{order_id}",
                                     f"{ROOT_PATH}/orders/{order_id}/invoice/issue")
    inv_date = invoice["invoice_date"]
    invoice_no = invoice.get("invoice_no") or invoice_series.legacy_eu(order)
    # Use the EUR rate from the order itself (at time of purchase) for consistency
    # with the stored order_total text values
    order_eur_rate = float(order.get("currency_value") or 0)
    if order_eur_rate <= 0:
        order_eur_rate = float(order.get("eur_rate") or 0.0108)
    # INR subtotal (sum of product totals) and EUR conversion
    inr_subtotal = 0.0
    for p in order.get("products", []):
        p["price_eur"] = round(float(p["price"]) * order_eur_rate, 2)
        p["total_eur"] = round(float(p["total"]) * order_eur_rate, 2)
        p["price_inr"] = round(float(p["price"]), 2)
        p["total_inr"] = round(float(p["total"]), 2)
        inr_subtotal += float(p["total"])
    order["total_eur"] = round(float(order["total"]) * order_eur_rate, 2)
    # Export GST: 5% on INR subtotal
    inr_subtotal = round(inr_subtotal, 2)
    gst_rate = 5
    gst_amount = round(inr_subtotal * gst_rate / 100, 2)
    inr_grand_total = round(inr_subtotal + gst_amount, 2)
    order["inr_subtotal"] = inr_subtotal
    order["gst_rate"] = gst_rate
    order["gst_amount"] = gst_amount
    order["inr_grand_total"] = inr_grand_total
    irn_info = einvoice.get_irn_status_summary(order_id)

    # Fetch LOT numbers and UPC for EU customs compliance
    from database import db_cursor, inv_cursor
    lot_data = {}
    with db_cursor() as cur:
        cur.execute("""
            SELECT order_product_id, product_id, product_name, lot_number, upc, unit_number, country_of_origin
            FROM order_lot_numbers WHERE order_id = %s ORDER BY order_product_id, unit_number
        """, (order_id,))
        for row in cur.fetchall():
            op_id = row["order_product_id"]
            if op_id not in lot_data:
                lot_data[op_id] = {"lots": [], "upc": row["upc"], "product_id": row["product_id"],
                                   "country_of_origin": row.get("country_of_origin", "")}
            lot_data[op_id]["lots"].append(row["lot_number"])
            if row["upc"] and not lot_data[op_id]["upc"]:
                lot_data[op_id]["upc"] = row["upc"]
            if row.get("country_of_origin") and not lot_data[op_id]["country_of_origin"]:
                lot_data[op_id]["country_of_origin"] = row["country_of_origin"]

    # Try to fetch country_of_origin from inventory DB if not in local lot_data
    for p in order.get("products", []):
        op_id = p["order_product_id"]
        ld = lot_data.get(op_id, {})
        if not ld.get("country_of_origin"):
            # Try inventory DB
            try:
                with inv_cursor() as cur:
                    cur.execute("SELECT country_of_origin FROM products WHERE name = %s LIMIT 1", (p["name"],))
                    row = cur.fetchone()
                    if row and row.get("country_of_origin"):
                        if op_id in lot_data:
                            lot_data[op_id]["country_of_origin"] = row["country_of_origin"]
                        else:
                            lot_data[op_id] = {"lots": [], "upc": "", "product_id": p["product_id"],
                                               "country_of_origin": row["country_of_origin"]}
            except Exception:
                pass
        p["country_of_origin"] = lot_data.get(op_id, {}).get("country_of_origin", "")

    # Check if any product is missing country_of_origin
    missing_origin = [p for p in order.get("products", []) if not p.get("country_of_origin")]

    # Merge identical products (same product_id + unit price) into a single display
    # line for the Product table — e.g. left-eye + right-eye of the same SKU become one
    # Qty-N line. Per-eye Rx options are concatenated. The customs identifiers block
    # below stays per box (per UPC/LOT) as EU Reg 2025/842 requires.
    line_items = []
    _grouped = {}
    for p in order.get("products", []):
        key = (p.get("product_id"), round(float(p.get("price") or 0), 4))
        g = _grouped.get(key)
        if g is None:
            g = {
                "name": p["name"],
                "model": p.get("model"),
                "quantity": 0,
                "country_of_origin": p.get("country_of_origin", ""),
                "price_eur": p.get("price_eur", 0.0),
                "total_eur": 0.0,
                "price_inr": p.get("price_inr", p.get("price")),
                "total_inr": 0.0,
                "options": [],
            }
            _grouped[key] = g
            line_items.append(g)
        g["quantity"] += int(p.get("quantity") or 0)
        g["total_eur"] = round(g["total_eur"] + float(p.get("total_eur") or 0), 2)
        g["total_inr"] = round(g["total_inr"] + float(p.get("total_inr", p.get("total")) or 0), 2)
        if not g["country_of_origin"]:
            g["country_of_origin"] = p.get("country_of_origin", "")
        for opt in (p.get("options") or []):
            g["options"].append(opt)

    # Build product identifiers for customs
    product_identifiers = []
    for p in order.get("products", []):
        op_id = p["order_product_id"]
        ld = lot_data.get(op_id, {})
        upc = ld.get("upc", "")
        lots = ld.get("lots", [])
        pid = ld.get("product_id", p["product_id"])
        product_identifiers.append({
            "name": p["name"],
            "c127": str(pid),
            "c128": ", ".join([l for l in lots if l]) if lots else "",
            "c129": upc if upc else "",
            "y081": "NO" if not upc else "",
        })

    # Choose invoice style by shipping destination:
    #   EU-27 (excl. UK) -> full EU-compliance invoice (customs sections + INR GST page)
    #   all others (incl. UK) -> plain old-style EUR customer invoice + INR GST page
    EU_27_ISO = {
        "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
        "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
        "SI", "ES", "SE",
    }
    ship_iso = ""
    try:
        with db_cursor() as cur:
            cur.execute("SELECT iso_code_2 FROM country WHERE country_id = %s LIMIT 1",
                        (order.get("shipping_country_id"),))
            row = cur.fetchone()
            if row and row.get("iso_code_2"):
                ship_iso = row["iso_code_2"].strip().upper()
    except Exception:
        ship_iso = ""
    is_eu = ship_iso in EU_27_ISO
    template_name = "pages/invoice.html" if is_eu else "pages/invoice_plain.html"

    # US shipments: show FDA details in the product cell in place of Rx options.
    is_us = ship_iso == "US"
    fda_lines, fda_missing, fda_choices = [], [], []
    fda_by_op = {}
    if is_us:
        fda_lines, fda_missing, fda_choices = fda_svc.resolve_for_order(
            order_id, order.get("products", []))
        fda_by_op = {fl["line"]["order_product_id"]: fl["fda"]
                     for fl in fda_lines if fl["fda"]}

    return templates.TemplateResponse(request=request, name=template_name,
        context={"request": request, "order": order, "base": ROOT_PATH,
                 "irn_info": irn_info, "lot_data": lot_data,
                 "product_identifiers": product_identifiers,
                 "line_items": line_items,
                 "missing_origin": missing_origin,
                 "is_eu": is_eu, "ship_iso": ship_iso,
                 "is_us": is_us, "fda_lines": fda_lines,
                 "fda_missing": fda_missing, "fda_choices": fda_choices,
                 "fda_by_op": fda_by_op, "invoice_date": invoice_dates.fmt(inv_date),
                 "invoice_no": invoice_no})


@app.post("/orders/{order_id}/invoice/issue")
async def order_invoice_issue(request: Request, order_id: int,
                              choice: str = Form(""), print_date: str = Form(""),
                              user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    dest = f"{ROOT_PATH}/orders/{order_id}/invoice"
    if invoice_dates.get_eu_invoice_date(order_id):
        return RedirectResponse(dest, status_code=303)
    issue_date, source, error = invoice_dates.resolve_choice(
        choice, print_date, order.get("date_added"))
    if error:
        return _invoice_issue_prompt(request, f"E-{order_id}", order,
                                     f"{ROOT_PATH}/orders/{order_id}",
                                     f"{ROOT_PATH}/orders/{order_id}/invoice/issue",
                                     error=error, entered=print_date)
    if invoice_dates.issue_eu_invoice_date(order_id, issue_date, source, user["id"]):
        invoice = invoice_dates.get_eu_invoice(order_id)
        auth.log_activity(user["id"], "invoice_issued", "order", order_id,
                          {"invoice_date": issue_date.isoformat(), "source": source,
                           "invoice_no": invoice.get("invoice_no") if invoice else None},
                          request.client.host)
    return RedirectResponse(dest, status_code=303)


# ═══════════════════════════════════════════
# COUNTRY OF ORIGIN (manual entry for invoice)
# ═══════════════════════════════════════════
@app.post("/orders/{order_id}/set-origin")
async def set_country_of_origin(request: Request, order_id: int,
                                user: dict = Depends(get_current_user)):
    """Save manually entered country of origin for products in this order."""
    from database import db_cursor
    form = await request.form()
    for key, val in form.items():
        if key.startswith("origin_") and val.strip():
            op_id = int(key.replace("origin_", ""))
            # Update all LOT entries for this order_product_id
            with db_cursor() as cur:
                cur.execute("""
                    UPDATE order_lot_numbers SET country_of_origin = %s
                    WHERE order_id = %s AND order_product_id = %s
                """, (val.strip(), order_id, op_id))
                # If no LOT entries exist yet, insert a placeholder row
                if cur.rowcount == 0:
                    # Get product info from order
                    cur.execute("""
                        SELECT product_id, name FROM order_product
                        WHERE order_product_id = %s AND order_id = %s
                    """, (op_id, order_id))
                    prod = cur.fetchone()
                    if prod:
                        cur.execute("""
                            INSERT INTO order_lot_numbers
                            (order_id, order_product_id, unit_number, product_id, product_name, country_of_origin, entered_by, entered_at)
                            VALUES (%s, %s, 0, %s, %s, %s, %s, NOW())
                        """, (order_id, op_id, prod["product_id"], prod["name"], val.strip(), user["username"]))
    return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}/invoice", status_code=302)


# ═══════════════════════════════════════════
# FDA / US CUSTOMS DETAILS (per product_id)
# ═══════════════════════════════════════════
@app.get("/fda", response_class=HTMLResponse)
async def fda_page(request: Request, user: dict = Depends(require_admin),
                   msg: str = None):
    records = fda_svc.list_all()
    return render("pages/fda_details.html", request, user,
                  records=records, msg=msg)


@app.post("/fda/add")
async def fda_add(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    fda_svc.add(
        product_id=int(form.get("product_id") or 0),
        product_name=(form.get("product_name") or "").strip(),
        manufacturer_name=(form.get("manufacturer_name") or "").strip(),
        manufacturer_country=(form.get("manufacturer_country") or "").strip(),
        manufacturer_address=(form.get("manufacturer_address") or "").strip(),
        registration_number=(form.get("registration_number") or "").strip(),
        fda_510k_number=(form.get("fda_510k_number") or "").strip(),
        pma_number=(form.get("pma_number") or "").strip(),
        classification_product_code=(form.get("classification_product_code") or "").strip(),
        created_by=user["username"],
    )
    return RedirectResponse(f"{ROOT_PATH}/fda?msg=FDA+record+added", status_code=302)


@app.post("/fda/{fda_id}/edit")
async def fda_edit(request: Request, fda_id: int,
                   user: dict = Depends(require_admin)):
    form = await request.form()
    fda_svc.update(
        fda_id=fda_id,
        manufacturer_name=(form.get("manufacturer_name") or "").strip(),
        manufacturer_country=(form.get("manufacturer_country") or "").strip(),
        manufacturer_address=(form.get("manufacturer_address") or "").strip(),
        registration_number=(form.get("registration_number") or "").strip(),
        fda_510k_number=(form.get("fda_510k_number") or "").strip(),
        pma_number=(form.get("pma_number") or "").strip(),
        classification_product_code=(form.get("classification_product_code") or "").strip(),
        is_active=1 if form.get("is_active") else 0,
        modified_by=user["username"],
    )
    return RedirectResponse(f"{ROOT_PATH}/fda?msg=FDA+record+updated", status_code=302)


@app.post("/orders/{order_id}/set-fda")
async def set_fda(request: Request, order_id: int,
                  user: dict = Depends(get_current_user)):
    """From the invoice page: create missing FDA records and/or pick which
    existing record applies to each order line, then return to the invoice."""
    form = await request.form()
    order = orders.get_order(order_id)
    prod_by_op = {p["order_product_id"]: p for p in (order.get("products", []) if order else [])}

    # New FDA records entered inline for products that had none.
    for key in list(form.keys()):
        if key.startswith("new_mfr_name_"):
            op_id = int(key.replace("new_mfr_name_", ""))
            mfr = (form.get(f"new_mfr_name_{op_id}") or "").strip()
            if not mfr:
                continue
            p = prod_by_op.get(op_id)
            if not p:
                continue
            new_id = fda_svc.add(
                product_id=p["product_id"],
                product_name=p["name"],
                manufacturer_name=mfr,
                manufacturer_country=(form.get(f"new_mfr_country_{op_id}") or "").strip(),
                manufacturer_address=(form.get(f"new_mfr_address_{op_id}") or "").strip(),
                registration_number=(form.get(f"new_reg_{op_id}") or "").strip(),
                fda_510k_number=(form.get(f"new_510k_{op_id}") or "").strip(),
                pma_number=(form.get(f"new_pma_{op_id}") or "").strip(),
                classification_product_code=(form.get(f"new_code_{op_id}") or "").strip(),
                created_by=user["username"],
            )
            fda_svc.set_map(order_id, op_id, new_id, user["username"])

    # Explicit per-line selection when a product has multiple records.
    for key in list(form.keys()):
        if key.startswith("choose_fda_"):
            op_id = int(key.replace("choose_fda_", ""))
            fid = form.get(f"choose_fda_{op_id}")
            if fid:
                fda_svc.set_map(order_id, op_id, int(fid), user["username"])

    return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}/invoice", status_code=302)


# ═══════════════════════════════════════════
# E-INVOICE / IRN / E-WAY BILL
# ═══════════════════════════════════════════
@app.post("/orders/{order_id}/generate-irn")
async def generate_irn_route(request: Request, order_id: int,
                            user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    # Find latest AWB tracking number for transport document
    awb_tracking = ""
    for awb in order.get("awbs", []):
        if awb.get("awb_status") == "created" and awb.get("tracking_number"):
            awb_tracking = awb["tracking_number"]
            break
    try:
        invoice = invoice_dates.get_eu_invoice(order_id)
        if not invoice:
            raise einvoice.EInvoiceError(
                "Issue the invoice before generating the IRN.")
        awb_dt = datetime.now().strftime("%d/%m/%Y")
        result = einvoice.generate_irn(
            order, awb_tracking=awb_tracking, awb_date=awb_dt,
            invoice_no=invoice.get("invoice_no") or invoice_series.legacy_eu(order),
            invoice_date=invoice["invoice_date"],
            created_by=user["id"])
        auth.log_activity(user["id"], "irn_generated", "order", order_id,
            {"irn": result.get("irn"), "ewb_no": result.get("ewb_no")},
            request.client.host)
        msg = f"IRN+generated:+{result.get('irn','')[:20]}..."
        if result.get("ewb_no"):
            msg += f"&ewb=E-Way+Bill:+{result['ewb_no']}"
        return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}?msg={msg}", status_code=302)
    except einvoice.EInvoiceError as e:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=IRN+Error:+{str(e)[:100]}",
            status_code=302)


@app.post("/orders/{order_id}/cancel-irn")
async def cancel_irn_route(request: Request, order_id: int,
                           reason: str = Form("1"),
                           remark: str = Form("Order cancelled"),
                           user: dict = Depends(get_current_user)):
    irn_rec = einvoice.get_irn_for_order(order_id)
    if not irn_rec or irn_rec["status"] != "ACT":
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=No+active+IRN+found",
            status_code=302)
    try:
        einvoice.cancel_irn(order_id, irn_rec["irn"], reason, remark)
        auth.log_activity(user["id"], "irn_cancelled", "order", order_id,
            {"irn": irn_rec["irn"]}, request.client.host)
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?msg=IRN+cancelled",
            status_code=302)
    except einvoice.EInvoiceError as e:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=Cancel+Error:+{str(e)[:100]}",
            status_code=302)


@app.post("/orders/{order_id}/generate-ewb")
async def generate_ewb_route(request: Request, order_id: int,
                             user: dict = Depends(get_current_user)):
    irn_rec = einvoice.get_irn_for_order(order_id)
    if not irn_rec or irn_rec["status"] != "ACT":
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=No+active+IRN+found",
            status_code=302)
    if irn_rec.get("ewb_no"):
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=E-Way+Bill+already+exists:+{irn_rec['ewb_no']}",
            status_code=302)
    order = orders.get_order(order_id)
    awb_tracking = ""
    for awb in order.get("awbs", []):
        if awb.get("awb_status") == "created" and awb.get("tracking_number"):
            awb_tracking = awb["tracking_number"]
            break
    try:
        awb_dt = datetime.now().strftime("%d/%m/%Y")
        result = einvoice.generate_ewb_by_irn(
            order_id, irn_rec["irn"],
            awb_tracking=awb_tracking, awb_date=awb_dt)
        auth.log_activity(user["id"], "ewb_generated", "order", order_id,
            {"ewb_no": result.get("ewb_no"), "irn": irn_rec["irn"]},
            request.client.host)
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?msg=E-Way+Bill+generated:+{result.get('ewb_no','')}",
            status_code=302)
    except einvoice.EInvoiceError as e:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=EWB+Error:+{str(e)[:100]}",
            status_code=302)


@app.get("/orders/{order_id}/irn", response_class=JSONResponse)
async def get_irn_info(order_id: int, user: dict = Depends(get_current_user)):
    return einvoice.get_irn_status_summary(order_id)


@app.post("/orders/{order_id}/status")
async def update_status(request: Request, order_id: int,
                        status_id: int = Form(...), comment: str = Form(""),
                        notify_email: int = Form(0),
                        notify_whatsapp: int = Form(0),
                        notify_sms: int = Form(0),
                        user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)

    # Processing users cannot change to certain statuses without approval
    restricted_statuses = {7, 11, 12, 13, 22}  # Canceled, Refunded, Reversed, Chargeback, Partial Refund
    if user["role"] == "processing" and status_id in restricted_statuses:
        approvals.create_approval_request(order_id, user["id"], "edit_order",
            {"new_status_id": status_id, "comment": comment})
        auth.log_activity(user["id"], "approval_requested", "order", order_id,
            {"status_id": status_id}, request.client.host)
        return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}?msg=Approval+requested", status_code=302)

    notify_any = notify_email or notify_whatsapp or notify_sms
    orders.update_order_status(order_id, status_id, comment, notify_any,
                               added_by=user["username"])
    auth.log_activity(user["id"], "status_update", "order", order_id,
        {"status_id": status_id, "comment": comment}, request.client.host)

    # Send notifications via selected channels
    channels = []
    if notify_email:
        channels.append("email")
    if notify_whatsapp:
        channels.append("whatsapp")
    if notify_sms:
        channels.append("sms")

    notify_results = {}
    if channels:
        status_name = config.ORDER_STATUSES.get(status_id, str(status_id))
        notify_results = notifications.notify_order_status(
            order, status_name, comment, channels, status_id=status_id)

    result_msgs = []
    for ch, (ok, msg) in notify_results.items():
        result_msgs.append(f"{ch}:{'ok' if ok else msg}")
    result_str = ",".join(result_msgs) if result_msgs else ""
    redirect_msg = f"Status+updated" + (f"&notify={result_str}" if result_str else "")

    return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}?msg={redirect_msg}", status_code=302)


@app.post("/orders/{order_id}/alert-contact-failed")
async def alert_contact_failed(request: Request, order_id: int,
                               user: dict = Depends(get_current_user)):
    """FedEx couldn't contact the customer — alert them via Email + WhatsApp together."""
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)

    # FedEx AWB/tracking is REQUIRED — prefer a 'created' AWB, else any tracking number
    tracking = ""
    for a in (order.get("awbs") or []):
        if a.get("awb_status") == "created" and a.get("tracking_number"):
            tracking = a["tracking_number"]
    if not tracking:
        for a in (order.get("awbs") or []):
            if a.get("tracking_number"):
                tracking = a["tracking_number"]
    if not tracking:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?msg=Cannot+send+alert%3A+a+FedEx+AWB%2Ftracking+number+is+required.+Issue+the+AWB+first.",
            status_code=302)

    notify_results = notifications.notify_fedex_contact_failed(
        order, tracking=tracking, channels=["email", "whatsapp"],
        created_by=user["username"])

    # Add an audit note to order history (status unchanged)
    try:
        orders.update_order_status(order_id, order["order_status_id"],
            "FedEx unable to contact customer — alert sent (Email + WhatsApp).", 1,
            added_by=user["username"])
    except Exception as e:
        logging.warning("contact-failed history note failed for %s: %s", order_id, e)

    auth.log_activity(user["id"], "fedex_contact_failed_alert", "order", order_id,
        {"tracking": tracking}, request.client.host)

    result_msgs = [f"{ch}:{'ok' if ok else msg}" for ch, (ok, msg) in notify_results.items()]
    result_str = ",".join(result_msgs) if result_msgs else "no-recipient"
    return RedirectResponse(
        f"{ROOT_PATH}/orders/{order_id}?msg=FedEx+contact+alert+sent&notify={result_str}",
        status_code=302)


# ═══════════════════════════════════════════
# SHIPPING / AWB
# ═══════════════════════════════════════════
@app.get("/orders/{order_id}/ship", response_class=HTMLResponse)
async def ship_form(request: Request, order_id: int, user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    inr_subtotal = float(order.get("total_inr", 0))
    gst_rate = 5
    gst_amount = round(inr_subtotal * gst_rate / 100, 2)
    order["gst_amount"] = gst_amount
    order["inr_grand_total"] = round(inr_subtotal + gst_amount, 2)
    return render("pages/ship_order.html", request, user, order=order)


@app.post("/orders/{order_id}/ship")
async def ship_order(request: Request, order_id: int,
                     weight_kg: float = Form(0.25),
                     length_cm: int = Form(17),
                     width_cm: int = Form(17),
                     height_cm: int = Form(7),
                     customs_amount: float = Form(0),
                     currency: str = Form("INR"),
                     hs_code: str = Form("90013000"),
                     commodity_desc: str = Form("contact lens"),
                     country_mfg: str = Form("US"),
                     qty: int = Form(1),
                     user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)

    # Ensure gst_amount is set for template (needed if error re-renders page)
    inr_subtotal = sum(float(p.get("total_inr", p["total"])) for p in order.get("products", []))
    gst_rate = 5
    gst_amount = round(inr_subtotal * gst_rate / 100, 2)
    order["gst_amount"] = gst_amount
    order["inr_grand_total"] = round(inr_subtotal + gst_amount, 2)

    if not customs_amount:
        inr_sub = float(order.get("total_inr", order["total"]))
        customs_amount = round(inr_sub * 1.05, 2)

    ship_params = {
        "weight_kg": weight_kg, "length_cm": length_cm, "width_cm": width_cm,
        "height_cm": height_cm, "customs_amount": customs_amount, "currency": currency,
        "hs_code": hs_code, "commodity_desc": commodity_desc,
        "country_mfg": country_mfg, "qty": qty,
    }

    try:
        payload = fedex.build_shipment_payload(order, ship_params)
        fedex.check_required_emails(payload)
        token = fedex.get_token()

        # Validate address
        original_addr = payload["requestedShipment"]["recipients"][0]["address"]
        validated = fedex.validate_address(token, original_addr)
        payload["requestedShipment"]["recipients"][0]["address"] = validated

        # Get rate estimate
        rate = fedex.get_rate_estimate(token, payload)

        # Issue the invoice only once FedEx has accepted the address and rated
        # the shipment, so a booking that fails early uses no number.
        try:
            invoice_record = invoice_dates.get_eu_invoice(order_id)
            issued = False
            if not invoice_record:
                issued = invoice_dates.issue_eu_invoice_date(
                    order_id, dt.date.today(), "today", user["id"])
                invoice_record = invoice_dates.get_eu_invoice(order_id)
                if not invoice_record:
                    raise RuntimeError("Invoice metadata was not created")
            order["invoice_no"] = (
                invoice_record.get("invoice_no") or invoice_series.legacy_eu(order))
        except Exception as invoice_error:
            return render("pages/ship_order.html", request, user, order=order,
                          error=f"Couldn't issue the invoice: {invoice_error}")
        if issued:
            auth.log_activity(user["id"], "invoice_issued", "order", order_id,
                {"via": "fedex_booking", "invoice_no": order["invoice_no"]},
                request.client.host)
        validated_address = payload["requestedShipment"]["recipients"][0]["address"]
        payload = fedex.build_shipment_payload(order, ship_params)
        payload["requestedShipment"]["recipients"][0]["address"] = validated_address

        # Create shipment
        resp = fedex.create_shipment(token, payload)
        tracking, label_b64 = fedex.extract_tracking_and_label(resp)

        label_path = None
        if label_b64:
            label_path = fedex.save_label_pdf(label_b64, order_id, tracking or "unknown")

        # Save AWB record
        with db_cursor() as cur:
            cur.execute("""
                INSERT INTO shipping_awb (order_id, tracking_number, courier, awb_status,
                    label_pdf_path, fedex_response, created_by)
                VALUES (%s, %s, 'FedEx', 'created', %s, %s, %s)
            """, (order_id, tracking, label_path, json.dumps(resp), user["id"]))

        # Update order status to Shipped (3) and add history
        orders.update_order_status(order_id, 3,
            f"Shipped via FedEx. AWB: {tracking}", 1,
            tracking_number=tracking, awb=tracking or "",
            courier="FedEx", added_by=user["username"])

        auth.log_activity(user["id"], "awb_created", "order", order_id,
            {"tracking": tracking, "rate": rate}, request.client.host)

        # Notify customer via all available channels
        if tracking:
            ship_channels = ["email", "whatsapp", "sms"]
            notifications.notify_order_shipped(order, tracking, "FedEx", ship_channels)

        # Auto-generate IRN + E-Way Bill after AWB creation
        irn_msg = ""
        if tracking:
            try:
                awb_dt = datetime.now().strftime("%d/%m/%Y")
                irn_result = einvoice.generate_irn(
                    order, awb_tracking=tracking, awb_date=awb_dt,
                    invoice_no=order["invoice_no"],
                    invoice_date=invoice_record["invoice_date"],
                    created_by=user["id"])
                irn_msg = f"&irn={irn_result.get('irn','')[:20]}&ewb={irn_result.get('ewb_no','')}"
                auth.log_activity(user["id"], "irn_generated", "order", order_id,
                    {"irn": irn_result.get("irn"), "ewb_no": irn_result.get("ewb_no")},
                    request.client.host)
            except Exception as irn_err:
                logging.warning("Auto IRN generation failed for order %s: %s", order_id, irn_err)
                irn_msg = "&irn_error=Auto+IRN+generation+failed"

        return RedirectResponse(f"{ROOT_PATH}/orders/{order_id}?msg=AWB+created:+{tracking}{irn_msg}", status_code=302)

    except fedex.FedExError as e:
        return render("pages/ship_order.html", request, user, order=order, error=str(e), error_details=e.details)
    except Exception as e:
        return render("pages/ship_order.html", request, user, order=order, error=f"Unexpected error: {str(e)}")


@app.post("/awb/{awb_id}/cancel")
async def cancel_awb(request: Request, awb_id: int, user: dict = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM shipping_awb WHERE id = %s", (awb_id,))
        awb = cur.fetchone()
    if not awb:
        raise HTTPException(404)

    try:
        token = fedex.get_token()
        resp = fedex.cancel_shipment(token, awb["tracking_number"])
        with db_cursor() as cur:
            cur.execute("UPDATE shipping_awb SET awb_status = 'cancelled' WHERE id = %s", (awb_id,))
        auth.log_activity(user["id"], "awb_cancelled", "order", awb["order_id"],
            {"tracking": awb["tracking_number"]}, request.client.host)
        return RedirectResponse(f"{ROOT_PATH}/orders/{awb['order_id']}?msg=AWB+cancelled", status_code=302)
    except fedex.FedExError as e:
        return RedirectResponse(f"{ROOT_PATH}/orders/{awb['order_id']}?error={str(e)}", status_code=302)


# ─── Manual DTDC shipments (address book, not tied to an order) ───
def _manual_ship_page(request, user, form=None, **kw):
    return render("pages/manual_ship.html", request, user,
                  addresses=manual_ship_svc.list_addresses(),
                  shipments=manual_ship_svc.list_shipments(),
                  descriptions=manual_ship_svc.DESCRIPTIONS,
                  dtdc_ok=dtdc_svc.configured(), dtdc_live=config.DTDC_LIVE,
                  form=form or {}, **kw)


@app.get("/manual-ship", response_class=HTMLResponse)
async def manual_ship_page(request: Request, user: dict = Depends(get_current_user)):
    return _manual_ship_page(request, user)


@app.post("/manual-ship", response_class=HTMLResponse)
async def manual_ship_book(
    request: Request,
    name: str = Form(""), phone: str = Form(""), address_1: str = Form(""), address_2: str = Form(""),
    city: str = Form(""), state: str = Form(""), pincode: str = Form(""),
    address_id: str = Form(""), save_address: str = Form(""), label: str = Form(""),
    shipping_mode: str = Form("Express"), payment_mode: str = Form("Prepaid"),
    declared_value: float = Form(500.0), cod_amount: float = Form(0.0), weight_kg: float = Form(0.25),
    description: str = Form("Spectacle Frames"), purpose: str = Form(""),
    user: dict = Depends(get_current_user),
):
    form = dict(name=name, phone=phone, address_1=address_1, address_2=address_2, city=city, state=state,
                pincode=pincode, address_id=address_id, save_address=save_address, label=label,
                shipping_mode=shipping_mode, payment_mode=payment_mode, declared_value=declared_value,
                cod_amount=cod_amount, weight_kg=weight_kg, description=description, purpose=purpose)
    errs = manual_ship_svc.validate_address(form)
    if payment_mode == "COD" and cod_amount <= 0:
        errs.append("COD amount must be > 0 for COD")
    if description not in manual_ship_svc.DESCRIPTIONS:
        errs.append("Invalid products description")
    if save_address and not label.strip():
        errs.append("Give the address a label to save it in the address book")
    if not dtdc_svc.configured():
        errs.append("DTDC is not configured")
    if errs:
        return _manual_ship_page(request, user, form, error="; ".join(errs))

    addr_id = int(address_id) if address_id.strip().isdigit() else None
    if save_address:
        addr_id = manual_ship_svc.save_address(form, user["id"])
        form["address_id"] = addr_id
    opts = dict(shipping_mode=shipping_mode, payment_mode=payment_mode, declared_value=declared_value,
                cod_amount=cod_amount if payment_mode == "COD" else 0.0, weight_kg=weight_kg,
                description=description, purpose=purpose.strip()[:255])
    try:
        s = manual_ship_svc.book(form, opts, user["id"], addr_id)
    except dtdc_svc.DtdcError as e:
        return _manual_ship_page(request, user, form, error=str(e),
                                 error_details=json.dumps(e.details, indent=1)[:3000] if e.details else "")
    msg = (f"DTDC AWB <strong>{s['tracking_number']}</strong> booked for {s['consignee_name']} "
           f"({s['reference']}, {s['shipping_mode']}, {s['payment_mode']}).")
    if s.get("label_pdf_path"):
        msg += f' <a class="btn btn-sm btn-primary" href="{ROOT_PATH}/manual-ship/{s["id"]}/label">Download Label</a>'
    else:
        msg += " Label not ready yet — use the Label button below in a minute."
    return _manual_ship_page(request, user, {}, success=msg)


@app.post("/manual-ship/address/{address_id}/delete")
async def manual_ship_delete_address(address_id: int, user: dict = Depends(get_current_user)):
    manual_ship_svc.delete_address(address_id)
    return RedirectResponse(f"{ROOT_PATH}/manual-ship", status_code=303)


@app.get("/manual-ship/{shipment_id}/label")
async def manual_ship_label(shipment_id: int, user: dict = Depends(get_current_user)):
    s = manual_ship_svc.get_shipment(shipment_id)
    if not s or not s.get("tracking_number"):
        raise HTTPException(404, "Shipment not found")
    path = s.get("label_pdf_path") or ""
    if not (path and os.path.exists(path)):
        path = dtdc_svc.save_label_pdf(s["tracking_number"], s["reference"])
        if not path:
            raise HTTPException(404, "Label not available from DTDC yet — try again in a minute")
        with db_cursor() as cur:
            cur.execute("UPDATE manual_shipments SET label_pdf_path=%s WHERE id=%s", (path, shipment_id))
    return FileResponse(path, filename=f"AWB_{s['tracking_number']}.pdf", media_type="application/pdf")


@app.post("/manual-ship/{shipment_id}/cancel", response_class=HTMLResponse)
async def manual_ship_cancel(request: Request, shipment_id: int, user: dict = Depends(get_current_user)):
    try:
        manual_ship_svc.cancel(shipment_id)
    except dtdc_svc.DtdcError as e:
        return _manual_ship_page(request, user, error=f"Cancel failed: {e}")
    return RedirectResponse(f"{ROOT_PATH}/manual-ship", status_code=303)


@app.get("/awb/{awb_id}/label")
async def download_label(awb_id: int, user: dict = Depends(get_current_user)):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM shipping_awb WHERE id = %s", (awb_id,))
        awb = cur.fetchone()
    if not awb or not awb.get("label_pdf_path"):
        raise HTTPException(404, "Label not found")
    return FileResponse(awb["label_pdf_path"], filename=f"AWB_{awb['tracking_number']}.pdf",
                        media_type="application/pdf")


# ═══════════════════════════════════════════
# APPROVALS (Admin only)
# ═══════════════════════════════════════════
@app.get("/approvals", response_class=HTMLResponse)
async def approvals_page(request: Request, user: dict = Depends(require_admin)):
    pending = approvals.list_pending_approvals()
    return render("pages/approvals.html", request, user, pending=pending)


@app.post("/approvals/{req_id}/approve")
async def approve(request: Request, req_id: int, notes: str = Form(""),
                  user: dict = Depends(require_admin)):
    req = approvals.get_approval(req_id)
    if not req:
        raise HTTPException(404)

    approvals.approve_request(req_id, user["id"], notes)

    # Execute the approved action
    if req["action_type"] == "edit_order" and req.get("action_data"):
        data = req["action_data"] if isinstance(req["action_data"], dict) else json.loads(req["action_data"])
        if "new_status_id" in data:
            orders.update_order_status(req["order_id"], data["new_status_id"],
                data.get("comment", "") + f" [Approved by {user['username']}]",
                added_by=user["username"])

    auth.log_activity(user["id"], "approval_approved", "approval", req_id, ip=request.client.host)
    return RedirectResponse(f"{ROOT_PATH}/approvals?msg=Approved", status_code=302)


@app.post("/approvals/{req_id}/reject")
async def reject(request: Request, req_id: int, notes: str = Form(""),
                 user: dict = Depends(require_admin)):
    approvals.reject_request(req_id, user["id"], notes)
    auth.log_activity(user["id"], "approval_rejected", "approval", req_id, ip=request.client.host)
    return RedirectResponse(f"{ROOT_PATH}/approvals?msg=Rejected", status_code=302)


# ═══════════════════════════════════════════
# USER MANAGEMENT (Admin only)
# ═══════════════════════════════════════════
@app.get("/users", response_class=HTMLResponse)
async def users_page(request: Request, user: dict = Depends(require_admin)):
    with db_cursor() as cur:
        cur.execute("SELECT id, username, email, role, full_name, is_active, created_at FROM platform_users ORDER BY id")
        all_users = cur.fetchall()
    return render("pages/users.html", request, user, all_users=all_users)


@app.post("/users/create")
async def create_user(request: Request, username: str = Form(...), password: str = Form(...),
                      email: str = Form(...), role: str = Form(...),
                      full_name: str = Form(""),
                      user: dict = Depends(require_admin)):
    try:
        auth.create_user(username, password, email, role, full_name)
        auth.log_activity(user["id"], "user_created", "user", details={"username": username, "role": role},
                         ip=request.client.host)
    except Exception as e:
        return RedirectResponse(f"{ROOT_PATH}/users?error={str(e)}", status_code=302)
    return RedirectResponse(f"{ROOT_PATH}/users?msg=User+created", status_code=302)


# ═══════════════════════════════════════════
# ACTIVITY LOG
# ═══════════════════════════════════════════
@app.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request, user: dict = Depends(require_admin),
                        page: int = 1):
    offset = (page - 1) * 50
    with db_cursor() as cur:
        cur.execute("""
            SELECT al.*, pu.username
            FROM activity_log al
            LEFT JOIN platform_users pu ON al.user_id = pu.id
            ORDER BY al.created_at DESC
            LIMIT 50 OFFSET %s
        """, (offset,))
        logs = cur.fetchall()
    return render("pages/activity.html", request, user, logs=logs, page=page)


# ═══════════════════════════════════════════
# MERCHANT CENTER
# ═══════════════════════════════════════════
@app.get("/merchant", response_class=HTMLResponse)
async def merchant_page(request: Request, user: dict = Depends(require_admin),
                        search: str = "", category: str = "", eu: str = ""):
    products = merchant_svc.list_products(search=search, category_filter=category, eu_filter=eu)
    categories = merchant_svc.get_categories()
    stats = merchant_svc.get_product_stats()
    return render("pages/merchant.html", request, user, products=products,
            categories=categories, stats=stats, search=search,
            category_filter=category, eu_filter=eu)


@app.post("/merchant/toggle-eu")
async def merchant_toggle_eu(request: Request, user: dict = Depends(require_admin),
                              product_id: int = Form(...), value: int = Form(...)):
    ok = merchant_svc.toggle_eu_saleable(product_id, value)
    auth.log_activity(user["id"], f"merchant_eu_{'enable' if value else 'disable'}",
                      details=f"product_id={product_id}", ip=request.client.host)
    return JSONResponse({"ok": ok})


@app.post("/merchant/toggle-status")
async def merchant_toggle_status(request: Request, user: dict = Depends(require_admin),
                                  product_id: int = Form(...), value: int = Form(...)):
    ok = merchant_svc.toggle_product_status(product_id, value)
    auth.log_activity(user["id"], f"product_{'enable' if value else 'disable'}",
                      details=f"product_id={product_id}", ip=request.client.host)
    return JSONResponse({"ok": ok})


@app.post("/merchant/bulk-eu")
async def merchant_bulk_eu(request: Request, user: dict = Depends(require_admin)):
    body = await request.json()
    product_ids = [int(pid) for pid in body.get("product_ids", [])]
    value = int(body.get("value", 1))
    count = merchant_svc.bulk_set_eu_saleable(product_ids, value)
    auth.log_activity(user["id"], f"merchant_bulk_eu_{'enable' if value else 'disable'}",
                      details=f"{count} products", ip=request.client.host)
    return JSONResponse({"ok": True, "count": count})


@app.post("/merchant/regenerate-feeds")
async def merchant_regenerate_feeds(request: Request, user: dict = Depends(require_admin)):
    try:
        result = subprocess.run(
            ["python3", "/var/www/lbws/lensbazaar.asia/feeds/merchant_feed_pipeline.py", "--mode", "xml"],
            capture_output=True, text=True, timeout=60,
            cwd="/var/www/lbws/lensbazaar.asia/feeds"
        )
        # Read summary
        import json as _json
        summary_path = "/var/www/lbws/lensbazaar.asia/system/logs/merchant_feed_summary.json"
        with open(summary_path) as f:
            summary = _json.load(f)
        auth.log_activity(user["id"], "merchant_regenerate_feeds",
                          details=f"EU:{summary.get('eu_count',0)} NonEU:{summary.get('non_eu_count',0)}",
                          ip=request.client.host)
        return JSONResponse({
            "ok": True,
            "eu_count": summary.get("eu_count", 0),
            "non_eu_count": summary.get("non_eu_count", 0),
            "output": result.stdout[-500:] if result.stdout else ""
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})



# ═══════════════════════════════════════════
# SITE CONTENT (FAQ Editor)
# ═══════════════════════════════════════════
import re as _re

_FAQ_TPL_PATH = "/var/www/lbws/lensbazaar.asia/catalog/view/theme/default/template/common/home_faq.tpl"
_FAQ_TPL_PATH_MOBILE = "/var/www/lbws/lensbazaar.asia/catalog/view/theme_mobile/omtex-mobile-new/template/common/home_faq.tpl"


def _parse_faq():
    try:
        with open(_FAQ_TPL_PATH, "r") as f:
            text = f.read()
    except FileNotFoundError:
        return []
    items = _re.findall(r'<h3[^>]*>(.*?)</h3>\s*<p[^>]*>(.*?)</p>', text, _re.DOTALL)
    return [{"question": q.strip(), "answer": a.strip()} for q, a in items]


def _generate_faq_tpl(faqs):
    items_html = ""
    schema_entries = []
    for faq in faqs:
        q, a = faq["question"], faq["answer"]
        items_html += (
            '        <div class="faq-item" style="flex: 1 1 45%; min-width: 280px; '
            'background: #f9f9f9; padding: 18px; border-radius: 8px;">\n'
            '            <h3 style="font-size: 15px; font-weight: 600; color: #333; '
            f'margin: 0 0 8px;">{q}</h3>\n'
            '            <p style="font-size: 14px; color: #555; line-height: 1.6; '
            f'margin: 0;">{a}</p>\n'
            '        </div>\n        \n'
        )
        a_esc = a.replace('"', '&quot;').replace("\n", " ")
        q_esc = q.replace('"', '&quot;')
        schema_entries.append(
            '{"@type": "Question", "name": "' + q_esc + '", '
            '"acceptedAnswer": {"@type": "Answer", "text": "' + a_esc + '"}}'
        )
    schema_json = ", ".join(schema_entries)
    return (
        '<!-- Homepage FAQ Section with FAQ Schema for AEO -->\n'
        '<div class="homepage-faq-section" style="max-width: 1200px; margin: 40px auto 20px; padding: 0 15px;">\n'
        '    <h2 style="font-size: 22px; font-weight: 600; margin-bottom: 20px; color: #333; text-align: center;">Frequently Asked Questions</h2>\n'
        '    <div class="faq-grid" style="display: flex; flex-wrap: wrap; gap: 20px;">\n'
        + items_html
        + '    </div>\n'
        '</div>\n'
        '<script type="application/ld+json">\n'
        '{"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [' + schema_json + ']}\n'
        '</script>\n'
    )



# ═══════════════════════════════════════════
# REPORTS
@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request, user: dict = Depends(require_admin)):
    from datetime import date, timedelta
    today = date.today()
    return render("pages/reports.html", request, user,
                  site="all",
                  from_date=(today - timedelta(days=30)).isoformat(),
                  to_date=today.isoformat(),
                  rows=None, summary=None)


@app.get("/reports/shipped")
async def reports_shipped(request: Request, user: dict = Depends(require_admin),
                          site: str = "all", from_date: str = "", to_date: str = "",
                          fmt: str = "html"):
    from fastapi.responses import Response
    if site not in ("all", "eu", "ow"):
        site = "all"
    site_label = {"all": "All Sites", "eu": "EU LensBazaar", "ow": "Optiwar"}[site]
    try:
        data = reports_svc.get_shipped_report(site, from_date, to_date)
    except Exception as e:
        return render("pages/reports.html", request, user, site=site,
                      from_date=from_date, to_date=to_date,
                      rows=None, summary=None, error_msg=f"Report error: {e}")
    rows = data["rows"]
    summary = data["summary"]
    if fmt == "csv":
        csv_text = reports_svc.build_csv(rows)
        fname = f"shipped_{site}_{from_date}_to_{to_date}.csv"
        return Response(content=csv_text, media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    if fmt == "pdf":
        pdf_bytes = reports_svc.build_pdf(rows, summary, site_label, from_date, to_date)
        fname = f"shipped_{site}_{from_date}_to_{to_date}.pdf"
        return Response(content=pdf_bytes, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{fname}"'})
    return render("pages/reports.html", request, user, site=site,
                  from_date=from_date, to_date=to_date,
                  rows=rows, summary=summary)


@app.get("/site-content", response_class=HTMLResponse)
async def site_content_page(request: Request, user: dict = Depends(require_admin), msg: str = None):
    faqs = _parse_faq()
    return render("pages/site_content.html", request, user, faqs=faqs, msg=msg)


@app.post("/site-content/save-faq")
async def save_faq(request: Request, user: dict = Depends(require_admin)):
    form = await request.form()
    questions = form.getlist("question")
    answers = form.getlist("answer")
    faqs = []
    for q, a in zip(questions, answers):
        if q.strip() and a.strip():
            faqs.append({"question": q.strip(), "answer": a.strip()})
    tpl_content = _generate_faq_tpl(faqs)
    with open(_FAQ_TPL_PATH, "w") as f:
        f.write(tpl_content)
    with open(_FAQ_TPL_PATH_MOBILE, "w") as f:
        f.write(tpl_content)
    # Clear OpenCart cache (desktop + mobile) so changes show immediately
    import glob as _glob
    for pattern in ["/var/www/lbws/lensbazaar.asia/system/cache/*.cache",
                    "/var/www/lbws/lensbazaar.asia/web/system/storage/cache/cache.*"]:
        for cf in _glob.glob(pattern):
            try:
                os.remove(cf)
            except OSError:
                pass
    auth.log_activity(user["id"], "faq_updated", "site_content", 0,
                      {"count": len(faqs)}, request.client.host)
    return RedirectResponse(
        f"{ROOT_PATH}/site-content?msg=FAQ+saved+successfully+({len(faqs)}+items)",
        status_code=302)


# ═══════════════════════════════════════════
# API ENDPOINTS (for AJAX)
# ═══════════════════════════════════════════
@app.get("/api/orders/{order_id}")
async def api_order(order_id: int, user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    # Convert datetime objects for JSON
    for key in list(order.keys()):
        if isinstance(order[key], datetime):
            order[key] = order[key].isoformat()
    return order


@app.get("/api/rate-estimate/{order_id}")
async def api_rate_estimate(order_id: int, user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(404)
    try:
        token = fedex.get_token()
        payload = fedex.build_shipment_payload(order, {})
        rate = fedex.get_rate_estimate(token, payload)
        return {"rate": rate}
    except Exception as e:
        return {"error": str(e)}



# ═══════════════════════════════════════════
# LENS PRICING MANAGEMENT (Optiwar)
# ═══════════════════════════════════════════
import httpx

OPTIWAR_PRICING_API = "https://optiwar.com/api/lens-pricing"
OPTIWAR_PRICING_UPDATE = "https://optiwar.com/api/lens-pricing/update"
OPTIWAR_PRICING_BULK = "https://optiwar.com/api/lens-pricing/bulk"
OPTIWAR_API_KEY = ""
_PRICING_REASON_UNSET = object()


def _pricing_response_reason(resp, data=_PRICING_REASON_UNSET):
    if data is _PRICING_REASON_UNSET:
        try:
            data = resp.json()
        except ValueError:
            data = None

    if isinstance(data, dict):
        reason = data.get("error") or data.get("message")
        text = str(reason) if reason else ""
    else:
        body = resp.text or ""
        paragraphs = re.findall(r"<p\b[^>]*>(.*?)</p\s*>", body, flags=re.I | re.S)
        if paragraphs:
            body = " ".join(paragraphs)
        text = re.sub(r"<[^>]*>", " ", body)
        text = unescape(text)

    return " ".join(str(text).split())[:200] or "no details"


@app.get("/lens-pricing", response_class=HTMLResponse)
def lens_pricing_page(request: Request, user: dict = Depends(require_admin)):
    """Lens pricing management page - fetches config from optiwar.com API."""
    pricing = {}
    pricing_json = "{}"
    error_msg = None
    try:
        resp = httpx.get(OPTIWAR_PRICING_API, timeout=10)
        if resp.status_code == 200:
            pricing = resp.json()
            pricing_json = json.dumps(pricing, indent=2)
        else:
            error_msg = f"Failed to fetch pricing from optiwar.com (HTTP {resp.status_code})"
    except Exception as e:
        error_msg = f"Cannot reach optiwar.com pricing API: {str(e)}"

    if not error_msg:
        error_msg = request.query_params.get("error")

    saved = request.query_params.get("saved") == "1"
    return render("pages/lens_pricing.html", request, user,
                  pricing=pricing, pricing_json=pricing_json,
                  error_msg=error_msg, saved=saved)


@app.post("/lens-pricing/update")
def lens_pricing_update(
    request: Request,
    context: str = Form(...),
    category: str = Form(...),
    code: str = Form(...),
    price: str = Form(...),
    price_eur: str = Form(""),
    user: dict = Depends(require_admin),
):
    """Update a single price item on optiwar.com (INR + EUR)."""
    try:
        payload = {"context": context, "category": category, "code": code, "price": price}
        if price_eur:
            payload["price_eur"] = price_eur
        resp = httpx.post(
            OPTIWAR_PRICING_UPDATE,
            data=payload,
            headers={"X-API-Key": OPTIWAR_API_KEY},
            timeout=10,
        )
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            reason = _pricing_response_reason(resp, data)
            return JSONResponse({
                "success": False,
                "error": f"Optiwar refused the save (HTTP {resp.status_code}): {reason}",
            })
        if resp.status_code != 200:
            reason = _pricing_response_reason(resp, data)
            return JSONResponse({
                "success": False,
                "error": f"Optiwar refused the save (HTTP {resp.status_code}): {reason}",
            })
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({
            "success": False,
            "error": f"Cannot reach optiwar.com: {e}",
        })


@app.post("/lens-pricing/bulk")
def lens_pricing_bulk(
    request: Request,
    pricing_json: str = Form(...),
    user: dict = Depends(require_admin),
):
    """Bulk update all pricing on optiwar.com."""
    try:
        resp = httpx.post(
            OPTIWAR_PRICING_BULK,
            data={"pricing_json": pricing_json},
            headers={"X-API-Key": OPTIWAR_API_KEY},
            timeout=10,
        )
        if resp.status_code == 200:
            return RedirectResponse(f"{ROOT_PATH}/lens-pricing?saved=1", status_code=303)
        else:
            reason = _pricing_response_reason(resp)
            error = f"Bulk save failed (HTTP {resp.status_code}): {reason}"
            return RedirectResponse(
                f"{ROOT_PATH}/lens-pricing?error={quote(error, safe='')}",
                status_code=303,
            )
    except Exception as e:
        error = f"Cannot reach optiwar.com: {e}"
        return RedirectResponse(
            f"{ROOT_PATH}/lens-pricing?error={quote(error, safe='')}",
            status_code=303,
        )


# ═══════════════════════════════════════════
# SUPPORT EMAIL (AI-DRAFTED)
# ═══════════════════════════════════════════
@app.get("/orders/{order_id}/support-email", response_class=HTMLResponse)
async def support_email_page(request: Request, order_id: int,
                             notes: str = Query(default=""),
                             followup: int = Query(default=0),
                             user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    previous = notifications.parse_email_details(
        notifications.get_notification_log(order_id, followup)) if followup else {}
    
    draft = None
    if notes.strip():
        try:
            draft = support_email.draft_support_email(order, notes.strip(),
                                                      previous=previous or None)
            # Escape body_html for hidden form field
            import html as html_mod
            draft["body_html_escaped"] = html_mod.escape(draft["body_html"])
        except Exception as e:
            logging.error("Support email draft failed for order %s: %s", order_id, e)
            return render("pages/support_email_draft.html", request, user,
                          order=order, draft=None, agent_notes=notes,
                          error=str(e), previous=previous,
                          followup_id=followup if previous else 0)
    
    return render("pages/support_email_draft.html", request, user,
                  order=order, draft=draft, agent_notes=notes,
                  previous=previous, followup_id=followup if previous else 0)


@app.post("/orders/{order_id}/support-email/send")
async def support_email_send(request: Request, order_id: int,
                             subject: str = Form(...),
                             body_html: str = Form(...),
                             to: str = Form(...),
                             agent_notes: str = Form(""),
                             extra_cc: str = Form(""),
                             followup_id: int = Form(0),
                             user: dict = Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    kind = "Follow-up support email" if followup_id else "Support email"
    
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    import html as html_mod
    
    # Unescape the body_html that was escaped for the form
    body_unescaped = html_mod.unescape(body_html)
    
    # Build CC list: always admin + any extra recipients
    cc_list = ["admin@lensbazaar.com"]
    if extra_cc.strip():
        for addr in extra_cc.split(","):
            addr = addr.strip()
            if addr and "@" in addr and addr not in cc_list:
                cc_list.append(addr)
    
    email_sent = False
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = config.SMTP_FROM or config.SMTP_USER
        msg["To"] = to
        msg["Subject"] = subject
        msg["Cc"] = ", ".join(cc_list)
        msg.attach(MIMEText(body_unescaped, "html"))
        recipients = [to] + cc_list
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASS)
            server.sendmail(config.SMTP_FROM or config.SMTP_USER,
                            recipients, msg.as_string())
        email_sent = True
        log_id = notifications._save_notification_log(
            order_id, "email", "support_email", to,
            True, f"{kind} sent. CC: {', '.join(cc_list)}. Subject: {subject}",
            user["username"],
            details=notifications.email_details(subject, body_unescaped, followup_of=followup_id))
        auth.log_activity(user["id"], "support_email_sent", "order", order_id,
            {"to": to, "cc": cc_list, "subject": subject, "notes": agent_notes[:200],
             "notification_id": log_id, "followup_of": followup_id or None},
            request.client.host)
    except Exception as e:
        logging.error("Failed to send support email for order %s: %s", order_id, e)
        notifications._save_notification_log(
            order_id, "email", "support_email", to,
            False, str(e), user["username"],
            details=notifications.email_details(subject, body_unescaped, followup_of=followup_id))
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}?error=Email+send+failed:+{str(e)[:80]}",
            status_code=302)
    
    # Update order status to show support action taken
    hist = f"{kind} sent to {to} - Subject: {subject}. CC: {', '.join(cc_list)}"
    if log_id:
        hist += f" (notification #{log_id}, full text on record)"
    orders.update_order_status(order_id, order["order_status_id"], hist,
        1 if email_sent else 0, added_by=user["username"])
    
    return RedirectResponse(
        f"{ROOT_PATH}/orders/{order_id}?msg=Support+email+sent+successfully",
        status_code=302)


# ═══════════════════════════════════════════

@app.get("/orders/{order_id}/support-email/view/{log_id}", response_class=HTMLResponse)
async def support_email_view(request: Request, order_id: int, log_id: int,
                             user: dict = Depends(get_current_user)):
    row = notifications.get_notification_log(order_id, log_id)
    if not row or row.get("channel") != "email":
        raise HTTPException(status_code=404, detail="Email log not found")
    return render("pages/support_email_view.html", request, user,
                  row=row, email=notifications.parse_email_details(row),
                  order_label=f"#E-{order_id}",
                  back_url=f"{ROOT_PATH}/orders/{order_id}",
                  view_base=f"{ROOT_PATH}/orders/{order_id}/support-email/view",
                  followup_url=f"{ROOT_PATH}/orders/{order_id}/support-email?followup={log_id}")


# ═══════════════════════════════════════════
# LOT NUMBERS (Per-unit scan workflow)
# ═══════════════════════════════════════════
@app.get("/orders/{order_id}/lot-numbers")
async def lot_numbers_page(request: Request, order_id: int, user=Depends(get_current_user),
                           msg: str = None, error: str = None, edit_mode: str = None):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    is_locked = _is_lot_locked(order_id)
    lot_entries = _get_lot_entries(order_id)
    products_summary = _get_products_summary(order, lot_entries)

    context = {
        "request": request, "user": user, "order": order,
        "lot_entries": lot_entries, "is_locked": is_locked, "edit_mode": False,
        "products_summary": products_summary,
        "message": msg or error, "msg_type": "error" if error else "success",
        "statuses": config.ORDER_STATUSES, "base": ROOT_PATH,
        "edit_mode": bool(edit_mode),
    }
    return templates.TemplateResponse(request=request, name="pages/lot_numbers.html", context=context)


@app.post("/orders/{order_id}/lot-numbers/scan")
async def lot_numbers_scan(request: Request, order_id: int, user=Depends(get_current_user)):
    """Add one scanned UPC + LOT number entry."""
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    form = await request.form()
    upc_val = form.get("upc", "").strip()
    lot_val = form.get("lot_number", "").strip()
    edit_mode = form.get("edit_mode", "")

    if not upc_val:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/lot-numbers?error=UPC+barcode+is+required",
            status_code=302)
    if not lot_val:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/lot-numbers?error=LOT+number+is+required",
            status_code=302)

    # Validate UPC against inventory DB — find matching product
    from database import db_cursor, inv_cursor
    matched_product = None
    try:
        with inv_cursor() as cur:
            cur.execute("SELECT product_id, UPC, name, PWR, CYL, AXIS FROM products WHERE UPC = %s LIMIT 1", (upc_val,))
            matched_product = cur.fetchone()
    except Exception as e:
        logging.warning("Could not validate UPC against inventory DB: %s", e)

    if not matched_product:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/lot-numbers?error=UPC+{upc_val}+not+found+in+inventory+database",
            status_code=302)

    # Find which order product(s) this UPC belongs to. A UPC can match more than
    # one line (e.g. two identical lines) — collect all candidates, then bind the
    # scan to the first candidate that still has boxes remaining.
    inv_name = matched_product["name"]

    def _line_matches(p):
        # exact, then normalized token-set match (word-order/plural/format tolerant),
        # then eu_inventory_map pack-map for upgraded/renamed SKUs.
        if p["name"] == inv_name:
            return True
        if _name_tokens_match(inv_name, p["name"]):
            return True
        if _get_pack_map(p["name"], upc_val):
            return True
        return False

    candidates = [p for p in order["products"] if _line_matches(p)]
    # single-line order → bind the valid UPC to the only line even if the name differs
    if not candidates and len(order["products"]) == 1:
        candidates = [order["products"][0]]

    if not candidates:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/lot-numbers?error=UPC+{upc_val}+does+not+match+any+product+in+this+order",
            status_code=302)

    # When several same-named lines match (e.g. left vs right eye), bind the UPC to
    # the line whose Rx power matches this UPC's inventory record (SPH/CYL/AXIS), so a
    # right-eye UPC can't land on the left-eye line. Fall back to all candidates if
    # the inventory record has no power stored or nothing matches.
    if len(candidates) > 1:
        inv_pwr = _to_num(matched_product.get("PWR"))
        inv_cyl = _to_num(matched_product.get("CYL"))
        inv_axis = _to_num(matched_product.get("AXIS"))
        rx_narrowed = [p for p in candidates
                       if _rx_matches_upc(p, inv_pwr, inv_cyl, inv_axis)]
        if rx_narrowed:
            candidates = rx_narrowed

    # Count real scans (unit_number > 0) per line so multi-line orders roll over.
    scanned_by_op = {}
    with db_cursor() as cur:
        cur.execute(
            "SELECT order_product_id, COUNT(*) AS c FROM order_lot_numbers "
            "WHERE order_id = %s AND unit_number > 0 GROUP BY order_product_id",
            (order_id,))
        for r in cur.fetchall():
            scanned_by_op[r["order_product_id"]] = r["c"]

    # Bind to the first matching line that still has boxes remaining.
    target_op = None
    for p in candidates:
        if scanned_by_op.get(p["order_product_id"], 0) < _expected_boxes(p):
            target_op = p
            break

    if not target_op:
        total_expected = sum(_expected_boxes(p) for p in candidates)
        label = candidates[0]["name"].replace(" ", "+")
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/lot-numbers?error=All+{total_expected}+boxes+of+{label}+already+scanned",
            status_code=302)

    # Determine unit number (next sequential for this order_product_id)
    with db_cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(unit_number), 0) as max_unit FROM order_lot_numbers WHERE order_id = %s AND order_product_id = %s",
            (order_id, target_op["order_product_id"]))
        row = cur.fetchone()
        next_unit = (row["max_unit"] if row else 0) + 1

    # Insert the entry
    with db_cursor() as cur:
        cur.execute("""
            INSERT INTO order_lot_numbers (order_id, order_product_id, unit_number, product_id, product_name, lot_number, upc, entered_by, entered_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (order_id, target_op["order_product_id"], next_unit,
              target_op["product_id"], target_op["name"], lot_val, upc_val, user["username"]))

    redir_url = f"{ROOT_PATH}/orders/{order_id}/lot-numbers?msg=Unit+{next_unit}+added:+{upc_val}"
    if edit_mode:
        redir_url += "&edit_mode=1"
    return RedirectResponse(redir_url, status_code=302)


@app.post("/orders/{order_id}/lot-numbers/save")
async def lot_numbers_save(request: Request, order_id: int, user=Depends(get_current_user)):
    """Lock all lot entries for this order."""
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    from database import db_cursor
    with db_cursor() as cur:
        cur.execute("UPDATE order_lot_numbers SET is_locked = 1 WHERE order_id = %s", (order_id,))

    return RedirectResponse(
        f"{ROOT_PATH}/orders/{order_id}/lot-numbers?msg=All+LOT+entries+locked+successfully",
        status_code=302)


@app.post("/orders/{order_id}/lot-numbers/unlock")
async def lot_numbers_unlock(request: Request, order_id: int, user=Depends(get_current_user)):
    order = orders.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    form = await request.form()
    passcode = form.get("passcode", "")

    if passcode != config.LOT_EDIT_PASSCODE:
        return RedirectResponse(
            f"{ROOT_PATH}/orders/{order_id}/lot-numbers?error=Invalid+passcode",
            status_code=302)

    # Unlock all entries
    from database import db_cursor
    with db_cursor() as cur:
        cur.execute("UPDATE order_lot_numbers SET is_locked = 0 WHERE order_id = %s", (order_id,))

    return RedirectResponse(
        f"{ROOT_PATH}/orders/{order_id}/lot-numbers?msg=Unlocked+for+editing&edit_mode=1",
        status_code=302)


@app.get("/orders/{order_id}/lot-numbers/delete/{entry_id}")
async def lot_numbers_delete(request: Request, order_id: int, entry_id: int, user=Depends(get_current_user),
                             edit_mode: str = None):
    """Delete a single LOT entry."""
    from database import db_cursor
    with db_cursor() as cur:
        cur.execute("DELETE FROM order_lot_numbers WHERE id = %s AND order_id = %s AND is_locked = 0",
                    (entry_id, order_id))

    redir_url = f"{ROOT_PATH}/orders/{order_id}/lot-numbers?msg=Entry+removed"
    if edit_mode:
        redir_url += "&edit_mode=1"
    return RedirectResponse(redir_url, status_code=302)


def _ensure_eu_inv_map_table():
    """Create the EU inventory/pack-mapping table if it doesn't exist."""
    from database import db_cursor
    with db_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS eu_inventory_map (
                id INT AUTO_INCREMENT PRIMARY KEY,
                order_match VARCHAR(255) NOT NULL,
                inventory_upc VARCHAR(64) NULL,
                boxes_per_unit INT NOT NULL DEFAULT 1,
                note VARCHAR(255) NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)


_LOT_NAME_STOPWORDS = {
    "for", "high", "low", "power", "powers", "the", "a", "an", "and", "of", "with",
    "pack", "lens", "lenses", "box", "boxes", "contact", "contacts", "day", "days",
    "monthly", "daily", "pk", "ct",
}


def _norm_name_tokens(s):
    """Normalize a product name into a set of significant tokens.
    Lowercases, drops parentheticals, strips punctuation, removes filler words,
    and singularizes trailing plurals so 'Torics' == 'Toric'."""
    import re
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)          # drop parentheticals e.g. "(for high powers)"
    s = re.sub(r"[^a-z0-9]+", " ", s)       # non-alphanumeric -> space
    toks = set()
    for t in s.split():
        if t in _LOT_NAME_STOPWORDS:
            continue
        if len(t) > 3 and t.endswith("s"):   # crude singularize: torics -> toric
            t = t[:-1]
        toks.add(t)
    return toks


def _name_tokens_match(inv_name, order_name):
    """True when the two names refer to the same product despite word-order/format
    differences (e.g. 'Biofinity XR Toric' vs 'Biofinity Torics XR (for high powers)').
    Requires every significant token of one name to be present in the other."""
    inv = _norm_name_tokens(inv_name)
    ordr = _norm_name_tokens(order_name)
    if not inv or not ordr:
        return False
    return inv.issubset(ordr) or ordr.issubset(inv)


def _to_num(v):
    """Parse a power/cyl/axis value ('-4.25', '165°', '') into a float, else None."""
    import re
    if v is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(v))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _line_rx(order_line):
    """Extract (power, cyl, axis) for an order line from its options.
    Each toric/RX line carries one eye's values (LEFT/RIGHT POWER, LCylinder, LAxis…)."""
    pwr = cyl = axis = None
    for opt in (order_line.get("options") or []):
        nm = (opt.get("name") or "").upper()
        num = _to_num(opt.get("value"))
        if num is None:
            continue
        if "AXIS" in nm:
            axis = num
        elif "CYL" in nm:
            cyl = num
        elif "POWER" in nm or "SPH" in nm:
            pwr = num
    return pwr, cyl, axis


def _rx_matches_upc(order_line, inv_pwr, inv_cyl, inv_axis):
    """True when the order line's Rx matches the inventory record's power for a UPC.
    Every power field present in the inventory record must equal the line's value."""
    lpwr, lcyl, laxis = _line_rx(order_line)
    for iv, lv in ((inv_pwr, lpwr), (inv_cyl, lcyl), (inv_axis, laxis)):
        if iv is not None:
            if lv is None or abs(lv - iv) > 0.001:
                return False
    # require at least one field to have actually been compared
    return any(v is not None for v in (inv_pwr, inv_cyl, inv_axis))


def _get_pack_map(order_line_name, upc=None):
    """Return the eu_inventory_map row matching an order line (by name substring)
    or the scanned UPC, else None."""
    try:
        _ensure_eu_inv_map_table()
        from database import db_cursor
        with db_cursor() as cur:
            cur.execute("SELECT * FROM eu_inventory_map")
            rows = cur.fetchall()
        name = (order_line_name or "").lower()
        for r in rows:
            m = (r["order_match"] or "").lower()
            if m and (m in name or name in m):
                return r
        if upc:
            for r in rows:
                if r.get("inventory_upc") and r["inventory_upc"] == upc:
                    return r
    except Exception as e:
        logging.warning("eu_inventory_map lookup failed: %s", e)
    return None


def _expected_boxes(order_line):
    """Boxes expected for an order line = qty × boxes_per_unit (map, default 1)."""
    qty = order_line.get("quantity", 1) or 1
    m = _get_pack_map(order_line.get("name"))
    mult = (m["boxes_per_unit"] if m and m.get("boxes_per_unit") else 1) or 1
    return qty * mult


def _get_lot_entries(order_id):
    """Get all LOT entries for an order."""
    from database import db_cursor
    with db_cursor() as cur:
        cur.execute("""
            SELECT oln.*, op.quantity as qty
            FROM order_lot_numbers oln
            JOIN order_product op ON op.order_product_id = oln.order_product_id AND op.order_id = oln.order_id
            WHERE oln.order_id = %s AND oln.unit_number > 0
            ORDER BY oln.order_product_id, oln.unit_number
        """, (order_id,))
        entries = cur.fetchall()
        # Add qty info
        for e in entries:
            if not e.get("qty"):
                e["qty"] = 1
        return entries


def _get_products_summary(order, lot_entries):
    """Get products with scan counts."""
    # Count scanned per order_product_id
    scanned_counts = {}
    for e in lot_entries:
        op_id = e["order_product_id"]
        scanned_counts[op_id] = scanned_counts.get(op_id, 0) + 1

    summary = []
    for p in order["products"]:
        expected = _expected_boxes(p)
        summary.append({
            "order_product_id": p["order_product_id"],
            "name": p["name"],
            "model": p["model"],
            "quantity": expected,
            "order_qty": p["quantity"],
            "scanned": scanned_counts.get(p["order_product_id"], 0),
        })
    return summary


def _is_lot_locked(order_id):
    """Check if any lot entry for this order is locked."""
    from database import db_cursor
    with db_cursor() as cur:
        cur.execute("SELECT COUNT(*) as cnt FROM order_lot_numbers WHERE order_id = %s AND is_locked = 1", (order_id,))
        row = cur.fetchone()
        return row["cnt"] > 0 if row else False


# ═══════════════════════════════════════════
# RECONCILIATION (read-only view of the recon ledger on the WMS box)
def _recon_page(request, user, date, run=None):
    """The reconciliation day page, with or without the output of a run."""
    from datetime import date as _date
    days = recon_svc.recent_days()
    if not date:
        date = (days[0]["d"].isoformat() if days else _date.today().isoformat())
    runner_up, dry_seen = recon_svc.runner_health()
    return render("pages/recon_day.html", request, user,
                  data=recon_svc.day(date), days=days, run=run,
                  runner_up=runner_up, dry_seen=dry_seen)


@app.get("/recon", response_class=HTMLResponse)
async def recon_day(request: Request, user: dict = Depends(require_admin),
                    date: str = ""):
    try:
        return _recon_page(request, user, date)
    except Exception as exc:
        return render("pages/error.html", request, user,
                      error=f"Reconciliation ledger unreachable: {exc}",
                      status_code=503)


@app.post("/recon/run", response_class=HTMLResponse)
async def recon_run(request: Request, user: dict = Depends(require_admin),
                    date: str = Form(...), mode: str = Form(...)):
    """Dry run or commit one shipping date. The runner does the writing."""
    if mode not in ("dry", "commit"):
        raise HTTPException(status_code=400, detail="unknown run mode")
    run = recon_svc.run_day(date, mode, user.get("username") or "ops")
    return _recon_page(request, user, date, run=run)


@app.get("/recon/order/{receivable_id}", response_class=HTMLResponse)
async def recon_order(request: Request, receivable_id: int,
                      user: dict = Depends(require_admin)):
    data = recon_svc.receivable(receivable_id)
    if not data:
        raise HTTPException(status_code=404, detail="No such receivable")
    return render("pages/recon_order.html", request, user, d=data)


@app.get("/recon/payout/{batch_id}", response_class=HTMLResponse)
async def recon_payout(request: Request, batch_id: int,
                       user: dict = Depends(require_admin)):
    data = recon_svc.payout(batch_id)
    if not data:
        raise HTTPException(status_code=404, detail="No such payout batch")
    return render("pages/recon_payout.html", request, user, d=data)


# ERROR HANDLERS
# ═══════════════════════════════════════════
@app.exception_handler(401)
async def auth_exception_handler(request: Request, exc: HTTPException):
    return RedirectResponse(f"{ROOT_PATH}/login", status_code=302)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse(f"{ROOT_PATH}/login", status_code=302)
    context = {"request": request, "error": exc.detail, "status_code": exc.status_code, "user": None, "statuses": config.ORDER_STATUSES}
    return templates.TemplateResponse(request=request, name="pages/error.html", context=context, status_code=exc.status_code)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.APP_HOST, port=config.APP_PORT, reload=True)


# ═══════════════════════════════════════════
# OPTIWAR ORDER ENTRY (read-only: emits SQL, never writes to optiwar2)
# ═══════════════════════════════════════════
def _ow_new_ctx(draft: dict):
    """Shared render context for the order-entry page."""
    data = draft["data"]
    site = data.get("site", "in.optiwar.com")
    cust = data.get("customer") or {}
    addresses = []
    rx_history = []
    if cust.get("mode") == "existing" and cust.get("customer_id"):
        addresses = ow_new.get_customer_addresses(int(cust["customer_id"]))
        rx_history = ow_new.get_customer_rx(int(cust["customer_id"]))
    lines = []
    for i, line in enumerate(data.get("lines") or []):
        row = dict(line)
        row["idx"] = i
        row["total"] = ow_new.line_total(line)
        lines.append(row)
    return {
        "draft": draft, "data": data, "site": site,
        "site_info": ow_new.SITES.get(site, ow_new.SITES["in.optiwar.com"]),
        "sites": ow_new.SITES, "addresses": addresses, "rx_history": rx_history,
        "lines": lines, "total": ow_new.draft_total(data),
        "lens_options": ow_new.lens_options(site),
        "lens_options_bifocal": ow_new.lens_options(site, bifocal_context=True),
        "errors": ow_new.validate_draft(data),
        "pay_links": rzp.links_for_order(draft["order_id"]) if draft.get("order_id") else [],
        "ow_write": ow_write_enabled(),
    }


@app.get("/ow-new-order", response_class=HTMLResponse)
def ow_new_order_list(request: Request, user: dict = Depends(get_current_user)):
    """Optiwar order-entry drafts."""
    return render("pages/ow_new_order_list.html", request, user,
                  drafts=ow_new.list_drafts())


@app.post("/ow-new-order/create")
def ow_new_order_create(request: Request, site: str = Form("in.optiwar.com"),
                        user: dict = Depends(get_current_user)):
    draft_id = ow_new.new_draft(user["id"], site if site in ow_new.SITES else "in.optiwar.com")
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)


def _ow_new_jsonable(row: dict) -> dict:
    """Prices come back as Decimal and dates as datetime — neither is JSON-safe."""
    out = {}
    for k, v in row.items():
        if isinstance(v, decimal.Decimal):
            out[k] = float(v)
        elif isinstance(v, (dt.datetime, dt.date)):
            out[k] = str(v)
        else:
            out[k] = v
    return out


@app.get("/ow-new-order/api/products")
def ow_new_api_products(q: str = Query(""), user: dict = Depends(get_current_user)):
    results = []
    for p in ow_new.search_products(q):
        row = _ow_new_jsonable(p)
        row["images"] = ow_new.image_urls(p.get("product_image"))
        row["size_label"] = ow_new.size_label(p)
        results.append(row)
    return JSONResponse({"results": results})


@app.get("/ow-new-order/api/customers")
def ow_new_api_customers(q: str = Query(""), user: dict = Depends(get_current_user)):
    return JSONResponse({"results": [_ow_new_jsonable(c) for c in ow_new.search_customers(q)]})


@app.get("/ow-new-order/{draft_id}", response_class=HTMLResponse)
def ow_new_order_page(request: Request, draft_id: int,
                      user: dict = Depends(get_current_user)):
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    return render("pages/ow_new_order.html", request, user, **_ow_new_ctx(draft))


@app.post("/ow-new-order/{draft_id}/site")
def ow_new_order_site(request: Request, draft_id: int, site: str = Form(...),
                      user: dict = Depends(get_current_user)):
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    data = draft["data"]
    if site in ow_new.SITES:
        data["site"] = site
        ow_new.save_draft(draft_id, data)
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)


@app.post("/ow-new-order/{draft_id}/customer")
def ow_new_order_customer(request: Request, draft_id: int,
                          mode: str = Form("new"), customer_id: str = Form(""),
                          name: str = Form(""), email: str = Form(""),
                          phone: str = Form(""),
                          user: dict = Depends(get_current_user)):
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    data = draft["data"]
    if mode == "existing" and customer_id.strip().isdigit():
        cust = ow_new.get_customer(int(customer_id))
        if cust:
            data["customer"] = {"mode": "existing", "customer_id": cust["customer_id"],
                                "name": cust["customer_name"] or "",
                                "email": cust["customer_email"] or "",
                                "phone": cust["customer_phone"] or ""}
            data["address"] = {"mode": "existing"}
    else:
        data["customer"] = {"mode": "new", "name": name.strip(),
                            "email": email.strip(), "phone": phone.strip()}
    ow_new.save_draft(draft_id, data)
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)


@app.post("/ow-new-order/{draft_id}/address")
def ow_new_order_address(request: Request, draft_id: int, mode: str = Form("new"),
                         address_id: str = Form(""), address: str = Form(""),
                         address2: str = Form(""), state: str = Form(""),
                         zipcode: str = Form(""), country: str = Form(""),
                         delivery_phone: str = Form(""), delivery_email: str = Form(""),
                         user: dict = Depends(get_current_user)):
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    data = draft["data"]
    if mode == "existing" and address_id.strip().isdigit():
        data["address"] = {"mode": "existing", "address_id": int(address_id)}
    else:
        data["address"] = {"mode": "new", "address": address.strip(),
                           "address2": address2.strip(), "state": state.strip(),
                           "zipcode": zipcode.strip(), "country": country.strip(),
                           "delivery_phone": delivery_phone.strip(),
                           "delivery_email": delivery_email.strip()}
    ow_new.save_draft(draft_id, data)
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)


@app.post("/ow-new-order/{draft_id}/line/add")
async def ow_new_order_line_add(request: Request, draft_id: int,
                                user: dict = Depends(get_current_user)):
    """Add one product line: product + price basis + prescription + lens add-ons."""
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    data = draft["data"]
    form = await request.form()
    pid = (form.get("product_id") or "").strip()
    if not pid.isdigit():
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}?err=no_product",
                                status_code=302)
    product = ow_new.get_product(int(pid))
    if not product:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}?err=bad_product",
                                status_code=302)
    prices = ow_new.product_price_options(product, data.get("site", "in.optiwar.com"))
    basis = form.get("price_basis") or "special"
    if basis == "manual":
        try:
            unit_price = float(form.get("manual_price") or 0)
        except ValueError:
            unit_price = 0.0
    else:
        unit_price = prices.get(basis) or 0.0
        if basis == "special" and not unit_price:
            unit_price = prices.get("list") or 0.0
            basis = "list"

    rx_mode = form.get("rx_mode") or "new"
    line = {
        "product_id": product["product_id"], "product_code": product["product_code"] or "",
        "product_name": product["product_name"] or "",
        "thumb": (ow_new.image_urls(product.get("product_image")) or [""])[0],
        "size_label": ow_new.size_label(product),
        "qty": max(1, int(form.get("qty") or 1)),
        "price_basis": basis, "unit_price": unit_price,
        "rx_mode": rx_mode,
    }
    if rx_mode == "existing":
        rx_id = (form.get("rx_id") or "").strip()
        line["rx_id"] = int(rx_id) if rx_id.isdigit() else None
        line["right_eye"] = form.get("rx_right_label") or ""
        line["left_eye"] = form.get("rx_left_label") or ""
        line["recommendations"] = form.get("rx_reco_label") or ""
        # The reused prescription keeps its own lens/add-on prices: the lens is being
        # made again for this order, so it is charged again. The rx row itself is only
        # referenced — the generated SQL never updates it.
        line["recommendation_price"] = 0
        line["addons"] = []
        cust = data.get("customer") or {}
        if line["rx_id"] and cust.get("customer_id"):
            for r in ow_new.get_customer_rx(int(cust["customer_id"])):
                if r["rx_id"] == line["rx_id"]:
                    line["recommendation_price"] = float(r.get("recommendation_price") or 0)
                    line["addons"] = [
                        {"name": r.get(f"addon_{i}_name"),
                         "price": float(r.get(f"addon_{i}_price") or 0)}
                        for i in (1, 2, 3) if r.get(f"addon_{i}_name")]
                    line["right_eye"] = r.get("right_eye") or line["right_eye"]
                    line["left_eye"] = r.get("left_eye") or line["left_eye"]
                    line["recommendations"] = r.get("recommendations") or line["recommendations"]
                    break
    else:
        # Stored the way checkout stores it — SPH/CYL/AXIS/ADD — so the
        # storefront, the lab sheet and the email all read the same prescription.
        line["right_eye"] = ow_new.normalize_rx_eye(form.get("right_eye"))
        line["left_eye"] = ow_new.normalize_rx_eye(form.get("left_eye"))
        opts = ow_new.lens_options(data.get("site", "in.optiwar.com"))
        opts_bi = ow_new.lens_options(data.get("site", "in.optiwar.com"), bifocal_context=True)
        design_code = form.get("lens_design") or ""
        design = next((o for o in opts.get("bifocal", []) if o["code"] == design_code), None)
        pool = opts_bi if design else opts
        addons = []
        for field in ("lens_coating", "lens_thickness"):
            code = form.get(field) or ""
            if not code:
                continue
            cat = "coatings" if field == "lens_coating" else "thickness"
            opt = next((o for o in pool.get(cat, []) if o["code"] == code), None)
            if opt:
                addons.append({"name": opt["name"], "price": opt["price"], "code": opt["code"]})
        reco_names = [design["name"]] if design else []
        reco_names += [a["name"] for a in addons]
        line["recommendations"] = ", ".join(reco_names)
        line["recommendation_price"] = design["price"] if design else 0
        line["addons"] = addons[:3]
    override = (form.get("total_override") or "").strip()
    line["total_override"] = override if override else None
    data.setdefault("lines", []).append(line)
    ow_new.save_draft(draft_id, data)
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)


@app.post("/ow-new-order/{draft_id}/line/{idx}/remove")
def ow_new_order_line_remove(request: Request, draft_id: int, idx: int,
                             user: dict = Depends(get_current_user)):
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    data = draft["data"]
    if 0 <= idx < len(data.get("lines") or []):
        data["lines"].pop(idx)
        ow_new.save_draft(draft_id, data)
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)


@app.post("/ow-new-order/{draft_id}/meta")
def ow_new_order_meta(request: Request, draft_id: int, note: str = Form(""),
                      pay_link: str = Form(""), is_test: str = Form(""),
                      user: dict = Depends(get_current_user)):
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    data = draft["data"]
    data["note"] = note.strip()
    data["pay_link"] = pay_link.strip()
    data["is_test"] = 1 if is_test else 0
    ow_new.save_draft(draft_id, data)
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)


@app.post("/ow-new-order/{draft_id}/generate")
def ow_new_order_generate(request: Request, draft_id: int,
                          user: dict = Depends(get_current_user)):
    """Generate the SQL script. Nothing is written to optiwar2."""
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    data = draft["data"]
    errors = ow_new.validate_draft(data)
    if errors:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}?err=incomplete",
                                status_code=302)
    order_id = draft.get("order_id") or ow_new.next_order_id()
    sql_text = ow_new.build_sql(data, order_id, actor=user.get("username", "ops"))
    ow_new.save_draft(draft_id, data, state="generated", order_id=order_id,
                      sql_text=sql_text, stamp="generated_at")
    auth.log_activity(user["id"], f"generated optiwar order SQL for {order_id} "
                                 f"(draft {draft_id}) — not executed",
                      "optiwar_order_sql", 0, request.client.host)
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)


@app.post("/ow-new-order/{draft_id}/create-in-optiwar")
def ow_new_order_create_live(request: Request, draft_id: int,
                             user: dict = Depends(require_admin)):
    """Create the order in optiwar2 from the dashboard, then read it back.

    The draft is claimed first, so a double-click cannot produce two orders. On
    any failure nothing is committed and the draft goes back to where it was."""
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    if not ow_write_enabled():
        ctx = _ow_new_ctx(draft)
        return render("pages/ow_new_order.html", request, user,
                      create_error="The optiwar2 write account is not configured on this "
                                   "box — run the SQL script instead.", **ctx)
    claim = ow_new.claim_draft_for_create(draft_id)
    if claim != "claimed":
        ctx = _ow_new_ctx(ow_new.get_draft(draft_id))
        return render("pages/ow_new_order.html", request, user,
                      create_error=("Another request is creating this order right now."
                                    if claim == "busy" else
                                    "This draft has already been created in optiwar2."),
                      **ctx)
    data = draft["data"]
    ok, res = ow_new.create_order(data, draft.get("order_id") or "",
                                  actor=user.get("username", "ops"))
    if not ok:
        ow_new.release_draft_claim(draft_id)
        auth.log_activity(user["id"], f"optiwar order creation failed for draft {draft_id}: "
                                      f"{res}", "optiwar_order", 0, request.client.host)
        ctx = _ow_new_ctx(ow_new.get_draft(draft_id))
        return render("pages/ow_new_order.html", request, user,
                      create_error=str(res), **ctx)
    order_id = res["order_id"]
    ow_new.save_draft(draft_id, data, state="verified", order_id=order_id,
                      sql_text=ow_new.build_sql(data, order_id,
                                                actor=user.get("username", "ops")),
                      stamp="verified_at")
    auth.log_activity(user["id"], f"created optiwar order {order_id} in optiwar2 "
                                  f"(draft {draft_id}, {len(data.get('lines') or [])} line(s), "
                                  f"total {ow_new.draft_total(data)})",
                      "optiwar_order", 0, request.client.host)
    ctx = _ow_new_ctx(ow_new.get_draft(draft_id))
    return render("pages/ow_new_order.html", request, user, verified=res,
                  verify_ran=True, created_now=order_id, **ctx)


@app.get("/ow-new-order/{draft_id}/sql")
def ow_new_order_sql(request: Request, draft_id: int,
                     user: dict = Depends(get_current_user)):
    draft = ow_new.get_draft(draft_id)
    if not draft or not draft.get("sql_text"):
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)
    fname = f"optiwar_order_{draft.get('order_id') or draft_id}.sql"
    return Response(content=draft["sql_text"], media_type="application/sql",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/ow-new-order/{draft_id}/verify")
def ow_new_order_verify(request: Request, draft_id: int,
                        user: dict = Depends(get_current_user)):
    """Read the order back from optiwar2 once the operator has run the script."""
    draft = ow_new.get_draft(draft_id)
    if not draft or not draft.get("order_id"):
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)
    found = ow_new.verify_order(draft["order_id"])
    if found:
        ow_new.save_draft(draft_id, draft["data"], state="verified", stamp="verified_at")
    ctx_data = _ow_new_ctx(ow_new.get_draft(draft_id))
    return render("pages/ow_new_order.html", request, user, verified=found,
                  verify_ran=True, **ctx_data)


def _ow_new_notify_order(draft: dict):
    """Build the notification payload from the draft + the live order."""
    data = draft["data"]
    site = data.get("site", "in.optiwar.com")
    info = ow_new.SITES.get(site, ow_new.SITES["in.optiwar.com"])
    cust = data.get("customer") or {}
    addr = data.get("address") or {}
    email = cust.get("email") or ""
    phone = cust.get("phone") or ""
    if addr.get("mode") == "existing" and addr.get("address_id"):
        for row in ow_new.get_customer_addresses(int(cust.get("customer_id") or 0)):
            if row["address_id"] == int(addr["address_id"]):
                email = row.get("delivery_email") or email
                phone = row.get("delivery_phone") or phone
                break
    else:
        email = addr.get("delivery_email") or email
        phone = addr.get("delivery_phone") or phone
    total = ow_new.draft_total(data)
    # right_eye/left_eye and the add-ons travel with each line so the email can
    # show the prescription the lens will be made to — the customer is being
    # asked to pay for it, so it has to be checkable.
    items = [{"name": l.get("product_name", ""), "code": l.get("product_code", ""),
              "qty": l.get("qty", 1), "lens": l.get("recommendations", ""),
              "right_eye": l.get("right_eye", ""), "left_eye": l.get("left_eye", ""),
              "addons": l.get("addons") or [], "symbol": info["symbol"],
              "total": f"{info['symbol']}{ow_new.line_total(l)}"}
             for l in (data.get("lines") or [])]
    name = (cust.get("name") or "").strip()
    return {"order_id": draft.get("order_id"), "firstname": name.split(" ")[0] if name else "",
            "email": email, "telephone": phone, "items": items,
            "total_display": f"{info['symbol']}{total}", "pay_link": data.get("pay_link", ""),
            "site": site, "note": data.get("note", "")}


@app.get("/ow-new-order/{draft_id}/notify-preview", response_class=HTMLResponse)
def ow_new_order_notify_preview(request: Request, draft_id: int,
                                user: dict = Depends(get_current_user)):
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    order = _ow_new_notify_order(draft)
    preview = notifications.notify_ow_new_order(order, dry_run=True)
    return render("pages/ow_new_order_notify.html", request, user, draft=draft,
                  order=order, preview=preview,
                  ready=(draft.get("state") == "verified"))


@app.post("/ow-new-order/{draft_id}/notify-send")
async def ow_new_order_notify_send(request: Request, draft_id: int,
                                   user: dict = Depends(get_current_user)):
    """Send the order-created notification. Only allowed once the order has been
    verified to exist in optiwar2 — otherwise the customer would be told about an
    order that was never created."""
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    if draft.get("state") != "verified":
        return RedirectResponse(
            f"{ROOT_PATH}/ow-new-order/{draft_id}/notify-preview?err=not_verified",
            status_code=302)
    form = await request.form()
    channels = form.getlist("channels")
    order = _ow_new_notify_order(draft)
    results = notifications.notify_ow_new_order(
        order, channels=channels,
        subject=form.get("subject", ""), body_html=form.get("body_html", ""),
        use_ai=False)
    ow_new.save_draft(draft_id, draft["data"], state="notified", stamp="notified_at")
    auth.log_activity(user["id"],
                      f"sent order-created notification for {draft.get('order_id')} "
                      f"via {', '.join(channels) or 'no channel'}",
                      "optiwar_order", 0, request.client.host)
    return render("pages/ow_new_order_notify.html", request, user, draft=draft,
                  order=order, preview=None, results=results, ready=True)


def _notify_ok(results) -> bool:
    """True when at least one channel actually delivered."""
    return bool(results) and any(r[0] for r in results.values())


@app.post("/ow-new-order/{draft_id}/payment-link")
def ow_new_order_payment_link(request: Request, draft_id: int,
                              user: dict = Depends(require_admin)):
    """Create a live Razorpay payment link for a verified ops order.

    Only allowed after verification: a link for an order that does not exist in
    optiwar2 would take money with nothing to fulfil."""
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    error = None
    pay_notified = None
    notice = None
    if draft.get("state") not in ("verified", "notified"):
        error = "Verify the order in optiwar2 before creating a payment link."
    else:
        data = draft["data"]
        site = data.get("site", "in.optiwar.com")
        info = ow_new.SITES.get(site, ow_new.SITES["in.optiwar.com"])
        order = _ow_new_notify_order(draft)
        cust = data.get("customer") or {}
        ok, res = rzp.create_link(
            draft["order_id"], ow_new.draft_total(data), info["currency"], site,
            name=cust.get("name", ""), email=order.get("email", ""),
            phone=order.get("telephone", ""), draft_id=draft_id,
            created_by=user.get("username", "ops"))
        if ok:
            # create_link hands back the stored row when it reuses an unpaid
            # link for the same amount, and a stored row is the only one with a
            # created_at.
            reused = "created_at" in res
            data["pay_link"] = res.get("short_url", "")
            ow_new.save_draft(draft_id, data)
            auth.log_activity(user["id"],
                              f"created razorpay payment link {res.get('link_id')} "
                              f"for {draft['order_id']} "
                              f"({info['currency']} {ow_new.draft_total(data)})",
                              "optiwar_order", 0, request.client.host)
            # A link the customer never receives is useless, so send it here
            # rather than leaving it to a second click. Claimed per link, so a
            # double-click or a reused link cannot message anyone twice.
            if rzp.claim_link_notify(res.get("link_id", "")):
                order["pay_link"] = data["pay_link"]
                pay_notified = notifications.notify_ow_new_order(
                    order, channels=["email", "whatsapp"], use_ai=False)
                if not _notify_ok(pay_notified):
                    rzp.release_link_notify(res.get("link_id", ""))
                auth.log_activity(
                    user["id"],
                    f"sent payment link for {draft['order_id']} via "
                    + ", ".join(f"{c}:{'ok' if r[0] else 'failed'}"
                                for c, r in pay_notified.items()),
                    "optiwar_order", 0, request.client.host)
            else:
                # The claim is refused when this link has already been sent, so
                # this click messaged nobody and created nothing. Saying so is
                # the whole point: a button that reports nothing at all reads as
                # broken, and the operator clicks it again.
                notice = (
                    f"No new link and nothing sent — this order already has "
                    f"{res.get('short_url', 'a payment link')}"
                    + (f", created {res['created_at']}" if reused else "")
                    + (f" and sent to the customer {res['notified_at']}"
                       if reused and res.get("notified_at") else "")
                    + ". Use Resend below to send that same link again, or "
                      "Cancel it and create a new one.")
        else:
            error = str(res)
    ctx = _ow_new_ctx(ow_new.get_draft(draft_id))
    return render("pages/ow_new_order.html", request, user, pay_error=error,
                  pay_notified=pay_notified, pay_notice=notice,
                  pay_notified_ok=_notify_ok(pay_notified), **ctx)


@app.post("/ow-new-order/{draft_id}/payment-link/{link_id}/resend")
def ow_new_order_payment_resend(request: Request, draft_id: int, link_id: str,
                                user: dict = Depends(require_admin)):
    """Send an existing link to the customer again — for a message that failed."""
    draft = ow_new.get_draft(draft_id)
    if not draft:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
    link = next((l for l in rzp.links_for_order(draft.get("order_id") or "")
                 if l["link_id"] == link_id), None)
    if not link:
        ctx = _ow_new_ctx(draft)
        return render("pages/ow_new_order.html", request, user,
                      pay_error="That payment link does not belong to this order.",
                      **ctx)
    order = _ow_new_notify_order(draft)
    order["pay_link"] = link["short_url"]
    sent = notifications.notify_ow_new_order(
        order, channels=["email", "whatsapp"], use_ai=False)
    if _notify_ok(sent):
        rzp.claim_link_notify(link_id)
    auth.log_activity(user["id"],
                      f"resent payment link {link_id} for {draft.get('order_id')} via "
                      + ", ".join(f"{c}:{'ok' if r[0] else 'failed'}"
                                  for c, r in sent.items()),
                      "optiwar_order", 0, request.client.host)
    ctx = _ow_new_ctx(ow_new.get_draft(draft_id))
    return render("pages/ow_new_order.html", request, user, pay_notified=sent,
                  pay_notified_ok=_notify_ok(sent), **ctx)


@app.post("/ow-new-order/{draft_id}/payment-link/{link_id}/refresh")
def ow_new_order_payment_refresh(request: Request, draft_id: int, link_id: str,
                                 user: dict = Depends(get_current_user)):
    """Poll Razorpay — covers a webhook that never arrived."""
    ok, res = rzp.refresh_link(link_id)
    notice = None
    if ok:
        notice = (f"Razorpay says this link is {res.get('status') or 'unknown'}"
                  + (f", payment {res['payment_id']}" if res.get("payment_id")
                     else " — no payment against it yet") + ".")
    ctx = _ow_new_ctx(ow_new.get_draft(draft_id))
    return render("pages/ow_new_order.html", request, user, pay_notice=notice,
                  pay_error=None if ok else str(res), **ctx)


@app.post("/ow-new-order/{draft_id}/payment-link/{link_id}/cancel")
def ow_new_order_payment_cancel(request: Request, draft_id: int, link_id: str,
                                user: dict = Depends(require_admin)):
    ok, res = rzp.cancel_link(link_id)
    auth.log_activity(user["id"], f"cancelled razorpay payment link {link_id}",
                      "optiwar_order", 0, request.client.host)
    ctx = _ow_new_ctx(ow_new.get_draft(draft_id))
    return render("pages/ow_new_order.html", request, user,
                  pay_error=None if ok else str(res), **ctx)


@app.get("/ow-new-order/{draft_id}/payment-sql/{payment_id}")
def ow_new_order_payment_sql(request: Request, draft_id: int, payment_id: str,
                             user: dict = Depends(get_current_user)):
    """SQL recording the payment on optiwar2 — for the operator to run."""
    draft = ow_new.get_draft(draft_id)
    if not draft or not draft.get("order_id"):
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)
    link = next((l for l in rzp.links_for_order(draft["order_id"])
                 if l.get("payment_id") == payment_id), None)
    if not link:
        return RedirectResponse(f"{ROOT_PATH}/ow-new-order/{draft_id}", status_code=302)
    sql_text = rzp.payment_sql(draft["order_id"], payment_id, link["amount_minor"],
                               link["currency"], link_id=link["link_id"])
    rzp.mark_sql_ready(payment_id)
    fname = f"optiwar_payment_{draft['order_id']}.sql"
    return Response(content=sql_text, media_type="application/sql",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/payments/razorpay/webhook")
async def razorpay_webhook(request: Request):
    """Razorpay callback. Signature-verified against the raw body and recorded in
    oslb6 only — optiwar2 is never written to from here; the operator runs the SQL
    from the order page."""
    raw = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if not rzp.verify_signature(raw, signature):
        logging.warning("razorpay webhook rejected: bad signature")
        return JSONResponse({"status": "rejected"}, status_code=400)
    try:
        body = json.loads(raw.decode())
    except ValueError:
        return JSONResponse({"status": "rejected"}, status_code=400)
    event = body.get("event", "")
    res = rzp.record_event(event, body, verified=True)
    if event in ("payment_link.paid", "payment.captured") and not res.get("duplicate"):
        if rzp.amount_matches(res.get("link_id"), res.get("amount_minor"),
                              res.get("currency")):
            logging.info("razorpay %s order:%s payment:%s — payment SQL ready",
                     event, res.get("order_id"), res.get("payment_id"))
        else:
            logging.error("razorpay %s amount/currency mismatch for link %s order %s",
                      event, res.get("link_id"), res.get("order_id"))
    return JSONResponse({"status": "ok"})


@app.post("/ow-new-order/{draft_id}/delete")
def ow_new_order_delete(request: Request, draft_id: int,
                        user: dict = Depends(require_admin)):
    ow_new.delete_draft(draft_id)
    return RedirectResponse(f"{ROOT_PATH}/ow-new-order", status_code=302)
