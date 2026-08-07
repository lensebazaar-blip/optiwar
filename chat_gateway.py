"""
Chat Gateway — Phase 1A
Custom AI chat API (self-contained; no external widget dependency).
MariaDB is the source of truth. Polling-based delivery.
"""
import os
import json
import uuid
import time
import re
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify, current_app, make_response, g, render_template
from itsdangerous import URLSafeSerializer, BadSignature
from openai import OpenAI
from . import acr
from .mail import create_ticket_in_db
import smtplib
from email.message import EmailMessage
from email.utils import formatdate
import hmac
import hashlib
import logging

bp = Blueprint('chat_gateway', __name__, url_prefix='/api/chat')

# ─── Module-level config (set by init_chat_gateway) ───
_config = {
    'deepseek_api_key': '',
    'llm_api_key': '',
    'llm_base_url': 'https://api.deepseek.com',
    'llm_model': 'deepseek-chat',
    'catalog_path': '',
    'knowledge_path': '',
    'mysql_config': {},
}

_catalog_cache = {'data': None, 'mtime': 0}
_knowledge_cache = {'data': None, 'mtime': 0}
_lens_rules_cache = {'data': None, 'mtime': 0}
_frame_rules_cache = {'data': None, 'mtime': 0}
_prescription_rules_cache = {'data': None, 'mtime': 0}
_lens_catalog_cache = {'data': None, 'mtime': 0}

# ─── KET Integration Auth (inbound push) ───
# KET Support pushes agent replies / resolutions to this app's webhook endpoints,
# authenticated with an HMAC-SHA256 signature over "<timestamp>:<raw_body>" using
# the shared OPTIWAR_WEBHOOK_SECRET. See _verify_ket_signature below.
KET_SIGNATURE_MAX_SKEW = 300  # seconds; reject stale/replayed timestamps


def _verify_ket_signature(raw_body, timestamp, signature):
    """Verify a KET webhook HMAC-SHA256 signature.

    signature == hex( HMAC_SHA256(secret, f"{timestamp}:{raw_body}") ), compared in
    constant time. Rejects a missing secret (fail closed), a missing/stale timestamp,
    or a mismatched signature. `raw_body` is the exact bytes/text KET sent.
    Returns True only on a valid, fresh signature.
    """
    secret = _config.get('ket_webhook_secret', '')
    if not secret:
        return False
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > KET_SIGNATURE_MAX_SKEW:
        return False
    if isinstance(raw_body, bytes):
        raw_body = raw_body.decode('utf-8', 'replace')
    expected = hmac.new(
        secret.encode('utf-8'),
        f"{timestamp}:{raw_body}".encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, str(signature))


def init_chat_gateway(app):
    """Initialize chat gateway with app config."""
    _config['deepseek_api_key'] = app.config.get('DEEPSEEK_API_KEY', '')
    _config['llm_api_key'] = app.config.get('LLM_API_KEY', '') or _config['deepseek_api_key']
    _config['llm_base_url'] = app.config.get('LLM_BASE_URL', 'https://api.deepseek.com')
    _config['llm_model'] = app.config.get('LLM_MODEL', 'deepseek-chat')
    # Explicit successor model + non-thinking mode for the legacy direct-SDK path
    # so it survives the 2026-07-24 retirement of the deepseek-chat alias.
    _config['deepseek_chat_model'] = app.config.get('DEEPSEEK_CHAT_MODEL', '') or _config['llm_model']
    _config['deepseek_thinking'] = str(app.config.get('AI_DEEPSEEK_THINKING', 'disabled')).lower()
    _config['ket_webhook_secret'] = app.config.get('OPTIWAR_WEBHOOK_SECRET', '')
    _config['catalog_path'] = os.path.join(app.root_path, 'static', 'ai', 'product_catalog.json')
    _config['knowledge_path'] = os.path.join(app.root_path, 'static', 'ai', 'optiwar_ai_knowledge_base.json')
    _config['lens_rules_path'] = os.path.join(app.root_path, 'static', 'ai', 'lens_rules.json')
    _config['frame_rules_path'] = os.path.join(app.root_path, 'static', 'ai', 'frame_rules.json')
    _config['prescription_rules_path'] = os.path.join(app.root_path, 'static', 'ai', 'prescription_rules.json')
    _config['lens_catalog_path'] = os.path.join(app.root_path, 'static', 'ai', 'lens_catalog.json')
    _config['mysql_config'] = {
        'host': app.config.get('MYSQL_HOST', 'localhost'),
        'user': app.config.get('MYSQL_USER', ''),
        'password': app.config.get('MYSQL_PASSWORD', ''),
        'database': app.config.get('MYSQL_DB', ''),
    }
    # ACR (A1/A2/A3): ensure the additive action/event tables exist. Best-effort
    # so a DB hiccup at boot never blocks app startup.
    try:
        acr.ensure_schema(_get_db)
    except Exception as e:  # noqa: BLE001
        app.logger.warning(f"[ACR] schema ensure skipped: {e}")


# ─── DB Helpers ───

def _get_db():
    """Get a DB connection (not using Flask g — direct connection for reliability)."""
    import MySQLdb
    import MySQLdb.cursors
    return MySQLdb.connect(
        host=_config['mysql_config']['host'],
        user=_config['mysql_config']['user'],
        password=_config['mysql_config']['password'],
        database=_config['mysql_config']['database'],
        cursorclass=MySQLdb.cursors.DictCursor,
        charset='utf8mb4',
        use_unicode=True,
        autocommit=True,
    )


def _log_event(db, session_id, event_type, payload=None):
    """Insert a chat_events row."""
    cur = db.cursor()
    cur.execute(
        """INSERT INTO chat_events (session_id, event_type, payload, created_at)
           VALUES (%s, %s, %s, NOW())""",
        (session_id, event_type, json.dumps(payload) if payload else None)
    )


def _insert_message(db, session_id, source, role, content, status='sent', metadata=None,
                    client_message_id=None):
    """Insert a chat_messages row. Returns the row id.

    When a client_message_id is supplied the write is idempotent on
    UNIQUE(session_id, source, client_message_id): a soft-retry (same id after a
    503) or a concurrent duplicate submit collapses to a single stored row and the
    existing row id is returned (via LAST_INSERT_ID). Rows without a
    client_message_id are always inserted (NULLs never collide in a unique index).
    """
    cur = db.cursor()
    cur.execute(
        """INSERT INTO chat_messages
               (session_id, source, role, content, status, metadata, client_message_id, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
           ON DUPLICATE KEY UPDATE id = LAST_INSERT_ID(id)""",
        (session_id, source, role, content, status,
         json.dumps(metadata) if metadata else None,
         client_message_id or None)
    )
    return cur.lastrowid


# ─── AI Engine ───

def _load_catalog():
    path = _config['catalog_path']
    try:
        mtime = os.path.getmtime(path)
        if _catalog_cache['data'] and _catalog_cache['mtime'] == mtime:
            return _catalog_cache['data']
        with open(path, 'r') as f:
            data = json.load(f)
        _catalog_cache['data'] = data
        _catalog_cache['mtime'] = mtime
        return data
    except Exception:
        return {'products': [], 'total_products': 0}


def _load_json_cached(path, cache):
    """Generic cached JSON loader."""
    try:
        mtime = os.path.getmtime(path)
        if cache['data'] and cache['mtime'] == mtime:
            return cache['data']
        with open(path, 'r') as f:
            data = json.load(f)
        cache['data'] = data
        cache['mtime'] = mtime
        return data
    except Exception:
        return None


def _load_knowledge():
    return _load_json_cached(_config['knowledge_path'], _knowledge_cache) or {}


def _load_lens_rules():
    return _load_json_cached(_config.get('lens_rules_path', ''), _lens_rules_cache) or {}


def _load_frame_rules():
    return _load_json_cached(_config.get('frame_rules_path', ''), _frame_rules_cache) or {}


def _load_lens_catalog():
    return _load_json_cached(_config.get('lens_catalog_path', ''), _lens_catalog_cache) or {}



def _build_catalog_summary(is_india=False):
    """Build a compact catalog summary for the system prompt.
    Instead of dumping 30 products, gives the AI an overview of what's available
    so it can formulate search_products() tool calls."""
    catalog = _load_catalog()
    products = catalog.get('products', [])
    in_stock = [p for p in products if p.get('qty', 0) > 0]

    if not in_stock:
        return ''

    from collections import Counter

    # Color distribution
    color_counts = Counter(p.get('color_filter', 'other') for p in in_stock)
    color_parts = [f"{c}: {n}" for c, n in color_counts.most_common()]

    # Shape distribution
    shape_counts = Counter()
    for p in in_stock:
        for s in (p.get('shapes') or []):
            shape_counts[s] += 1
    shape_parts = [f"{s}: {n}" for s, n in shape_counts.most_common()]

    # Facefit distribution
    facefit_counts = Counter((p.get('facefit') or 'unknown').upper() for p in in_stock)
    facefit_parts = [f"{f}: {n}" for f, n in facefit_counts.most_common()]

    # Price range
    currency = 'INR' if is_india else 'EUR'
    price_key = 'sale_inr' if is_india else 'sale_eur'
    prices = [float(p.get(price_key, 0) or 0) for p in in_stock if p.get(price_key)]
    min_p = min(prices) if prices else 0
    max_p = max(prices) if prices else 0

    summary = f"""
PRODUCT CATALOG SUMMARY ({len(in_stock)} frames in stock):
  Colors: {', '.join(color_parts)}
  Shapes: {', '.join(shape_parts)}
  Face fits: {', '.join(facefit_parts)}
  Price range: {min_p:.0f}-{max_p:.0f} {currency}
  All frames: Unisex, Made in India, can be configured as prescription sunglasses

TO FIND SPECIFIC PRODUCTS: Use the search_products tool with filters (color, shape, facefit, min_price, max_price, keyword).
The tool returns matching products with names, colors, prices, sizes, and URLs.
ALWAYS use search_products before recommending specific frames — do NOT guess or invent product details."""
    return summary



def _search_catalog(color=None, shape=None, facefit=None, min_price=None, max_price=None, keyword=None, is_india=False, limit=10):
    """Search the in-memory catalog with filters. Returns list of matching products."""
    catalog = _load_catalog()
    products = catalog.get('products', [])
    in_stock = [p for p in products if p.get('qty', 0) > 0]

    price_key = 'sale_inr' if is_india else 'sale_eur'
    results = []

    for p in in_stock:
        # Color filter
        if color:
            p_color = (p.get('color_filter') or '').lower()
            p_display = (p.get('color_display') or '').lower()
            p_raw = (p.get('color') or '').lower()
            color_lower = color.lower()
            if color_lower not in p_color and color_lower not in p_display and color_lower not in p_raw:
                continue

        # Shape filter
        if shape:
            p_shapes = [s.lower() for s in (p.get('shapes') or [])]
            if shape.lower() not in p_shapes:
                continue

        # Facefit filter
        if facefit:
            p_fit = (p.get('facefit') or '').lower()
            if facefit.lower() not in p_fit:
                continue

        # Price filter
        price = float(p.get(price_key, 0) or 0)
        if min_price and price < float(min_price):
            continue
        if max_price and price > float(max_price):
            continue

        # Keyword search (name, code, color_display)
        if keyword:
            kw = keyword.lower()
            searchable = f"{p.get('name','')} {p.get('code','')} {p.get('color_display','')} {p.get('description_seo','')}".lower()
            if kw not in searchable:
                continue

        results.append(p)

    # Sort by relevance (stock qty descending)
    results.sort(key=lambda x: x.get('qty', 0), reverse=True)
    results = results[:limit]

    # Format for AI consumption
    currency_sym = '\u20b9' if is_india else '\u20ac'
    formatted = []
    for p in results:
        price = p.get(price_key, '')
        formatted.append({
            'name': p.get('name', ''),
            'code': p.get('code', ''),
            'color': p.get('color_display', ''),
            'size': p.get('size', ''),
            'facefit': p.get('facefit', ''),
            'price': f"{price}{currency_sym}",
            'shapes': p.get('shapes', []),
            'url': p.get('url', ''),
            'qty': p.get('qty', 0),
        })

    return formatted



SEARCH_PRODUCTS_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search the Optiwar product catalog with optional filters. Use this whenever the customer asks about specific frames, colors, shapes, sizes, or prices. Returns matching products with details and URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "color": {
                        "type": "string",
                        "description": "Color filter. Values: black, blue, brown, red, green, pink, purple, gold, silver, white, orange, yellow, transparent, gray"
                    },
                    "shape": {
                        "type": "string",
                        "description": "Frame shape filter. Values: rectangle, oval, round, square, wayfarer, aviator, cateye, clubmaster, panto, kids, supra, rimless, horn, browline"
                    },
                    "facefit": {
                        "type": "string",
                        "description": "Face fit / size filter. Values: small, medium, large, extra large"
                    },
                    "min_price": {
                        "type": "number",
                        "description": "Minimum price filter (in customer's currency)"
                    },
                    "max_price": {
                        "type": "number",
                        "description": "Maximum price filter (in customer's currency)"
                    },
                    "keyword": {
                        "type": "string",
                        "description": "Free text keyword to search product names, codes, or descriptions"
                    }
                },
                "required": []
            }
        }
    }
]


def _build_system_prompt(contact_name, is_india=False, user_message='', customer_id=None):
    """Build system prompt for DeepSeek."""
    catalog_section = _build_catalog_summary(is_india)
    knowledge = _load_knowledge()
    lens_rules = _load_lens_rules()
    frame_rules = _load_frame_rules()
    lens_catalog = _load_lens_catalog()

    # Fetch customer's face measurements if available
    customer_measurements_section = ''
    if customer_id:
        try:
            meas_db = _get_db()
            meas_cur = meas_db.cursor()
            meas_cur.execute("""
                SELECT pd_far, face_width, recommended_length
                FROM face_measurements WHERE customer_id = %s
                ORDER BY measured_at DESC LIMIT 1
            """, (customer_id,))
            meas_row = meas_cur.fetchone()
            if meas_row:
                pd_val = float(meas_row['pd_far']) if meas_row['pd_far'] else None
                fw_val = float(meas_row['face_width']) if meas_row['face_width'] else None
                tl_val = int(meas_row['recommended_length'] or 0) if meas_row['recommended_length'] else None
                parts = []
                if pd_val: parts.append(f"PD: {pd_val}mm")
                if fw_val: parts.append(f"Face Width: {fw_val}mm")
                if tl_val: parts.append(f"Temple Length: {tl_val}mm")
                if parts:
                    customer_measurements_section = f"""
CUSTOMER'S FACE MEASUREMENTS (from AI Face Measurement tool):
  {', '.join(parts)}
  Recommended frame width: {int(fw_val) if fw_val else 'unknown'}mm
  Use these to recommend frames that match their size. Frame size format is lens_width-bridge-temple (e.g. 52-18-140).
  A frame MATCHES if: frame_total_width (lens_width*2 + bridge) is within ±8mm of face_width, AND temple is within ±10mm of temple_length.
"""
            meas_db.close()
        except Exception as e:
            current_app.logger.warning(f'[Chat] Failed to fetch measurements: {e}')

    # Build knowledge context (top relevant FAQs) — flat list structure
    faq_section = ''
    if knowledge:
        faqs = []
        for item in knowledge.get('faq', []):
            faqs.append(f"Q: {item.get('question','')}\nA: {item.get('answer','')}")
        if faqs:
            faq_section = '\nKNOWLEDGE BASE (top FAQs):\n' + '\n\n'.join(faqs[:25])

    # Build lens information section
    lens_section = ''
    if lens_rules:
        lens_types = lens_rules.get('lens_types', {})
        lens_lines = []
        for key, info in lens_types.items():
            name = info.get('name', key)
            desc = info.get('description', '')
            price_key = 'price_inr' if is_india else 'price_eur'
            price = info.get(price_key, '')
            currency_sym = '\u20b9' if is_india else '\u20ac'
            lens_lines.append(f"- {name}: {desc} ({currency_sym}{price} upgrade)")
        if lens_lines:
            lens_section = '\nLENS TYPES WE SELL:\n' + '\n'.join(lens_lines)

    # Build lens catalog (what we don't sell + power recommendations)
    lens_avail_section = ''
    if lens_catalog:
        dont_sell = lens_catalog.get('lenses_we_dont_sell', [])
        if dont_sell:
            lens_avail_section += f"\nLENSES WE DO NOT SELL: {', '.join(dont_sell)}. Always redirect to what we have."
        power_recs = lens_catalog.get('power_recommendations', {})
        if power_recs:
            rec_lines = [f"  {power}: {rec}" for power, rec in power_recs.items()]
            lens_avail_section += '\nLENS RECOMMENDATIONS BY POWER:\n' + '\n'.join(rec_lines)

    # Build frame fitting section
    frame_section = ''
    if frame_rules:
        face_to_frame = frame_rules.get('face_shape_to_frame_style', [])
        if face_to_frame:
            fit_lines = []
            if isinstance(face_to_frame, list):
                for item in face_to_frame:
                    face = item.get('face_shape', '')
                    styles = item.get('recommended_styles', [])
                    if face and styles:
                        fit_lines.append(f"  {face}: {', '.join(str(s) for s in styles[:4])}")
            elif isinstance(face_to_frame, dict):
                for face, styles in face_to_frame.items():
                    if isinstance(styles, list):
                        fit_lines.append(f"  {face}: {', '.join(str(s) for s in styles[:4])}")
            if fit_lines:
                frame_section = '\nFACE SHAPE \u2192 FRAME STYLE GUIDE (suggest AI Face Measurement at /tryon):\n' + '\n'.join(fit_lines)

    domain = 'optiwar.in' if is_india else 'optiwar.com'
    currency = 'INR (\u20b9)' if is_india else 'EUR (\u20ac)'

    prompt = f"""You are Optiwar AI Assistant \u2014 a direct, helpful eyewear shopping assistant for {domain}.
Customer name: {contact_name}
Currency: {currency}

RULES:
1. Be direct and brief. No long explanations unless customer explicitly asks for more details. Use the customer's name.
2. Recommend products ONLY from the catalog below. Never invent products.
3. Do NOT show raw URLs to the customer. Instead list products by name, color, size, and price. Offer to navigate: 'Would you like me to show you these? Click here to let me take you there' and include [ACTION:NAVIGATE:/eyeglasses/all-spectacle-frames.html?color=X&facefit=Y] at the end.
4. NEVER display full URLs (like https://optiwar.com/...) in your replies. The system will auto-navigate the customer when you include [ACTION:NAVIGATE:path]. Just say 'Let me take you there' or 'Click here to browse these'.
5. NEVER recommend out-of-stock products (qty:0). Skip them entirely from your recommendations. Only show in-stock items (qty > 0).
6. For prescription questions, refer to our guides at https://{domain}/ai-guide/prescription-reading
7. For PD measurement questions, refer to https://{domain}/ai-guide/pd-measurement
8. NEVER auto-escalate to a human. If the customer asks something you cannot answer OR explicitly asks for human support, first ASK: "Do you want me to connect you with my supervisor? Yes or No". Only add [ACTION:HUMAN_HANDOVER] if the customer explicitly says Yes. Do NOT escalate for informational questions you CAN answer (shipping, returns, policies, products).
9. For order issues or complaints, ask the customer first: "Would you like me to create a support ticket for this? Yes or No". Only add [ACTION:CREATE_TICKET] if they confirm Yes.
10. Keep responses under 50 words unless customer explicitly asks for details. Never dump paragraphs of info unprompted.
11. Format product recommendations as numbered lists with name, color, size, and price. No URLs in the text. End with 'Would you like me to take you to these frames?' and add [ACTION:NAVIGATE:...] with the appropriate filters.
12. If the customer mentions a specific SPH/CYL power, note that all frames include complimentary prescription lenses and recommend the appropriate lens thickness from LENS RECOMMENDATIONS BY POWER.
13. NEVER fabricate or invent product URLs. Only use the exact url field from the catalog. If a product has no url or url is null, do NOT generate a link. Instead direct the customer to: https://{domain}/eyeglasses/all-spectacle-frames.html
14. NEVER claim we sell something not in our product catalog or lens types list. If customer asks for a material/product we don't carry, say what we DO offer instead.
15. We do NOT sell: polycarbonate, trivex, CR-39, or glass lenses. Always redirect to our actual lens types.
16. The ONLY correct browse/category page URL is: https://{domain}/eyeglasses/all-spectacle-frames.html — use this exact URL whenever directing customers to browse frames. NEVER link to /categories/spectacles-frame (without a product slug) as it shows a sold-out error page. NEVER use the text 'spectacles frame category page' as a link.
17. When recommending frames, suggest the customer use our AI Face Measurement tool at https://{domain}/tryon for better fitting suggestions. Mention this once per session, not every message.
18. For high-power prescriptions (-6 and above), always recommend Ultra-Thin or Extra-Thin lenses and link to the lens page at https://{domain}/lenses
19. We DO NOT sell contact lenses on optiwar.com however our associated website EU LensBazaar  website https://eu.lensbazaar.com carries most range of brand contact lenses.
20. NAVIGATION: ALWAYS include [ACTION:NAVIGATE:url] at the END of your reply when you recommend frames or the customer asks to browse/see/go to a page.
    - For FRAMES with filters: [ACTION:NAVIGATE:/eyeglasses/all-spectacle-frames.html?PARAMS] — The ONLY valid URL params are: color, facefit, shape. NO other params (temple, pd, width, size, etc.) are supported.
      Valid color values: red/blue/black/green/brown/gold/silver/pink/purple/white/orange/yellow/transparent
      Valid facefit values: small/medium/large/extra+large (map from face_width: <128mm=small, 128-138mm=medium, 138-148mm=large, >148mm=extra+large)
      Valid shape values: round/rectangle/square/aviator/cateye/oval/clubmaster/wayfarer/kids/panto/supra/quatra/rimless
      Example: customer has face_width 134mm and wants pink frames → [ACTION:NAVIGATE:/eyeglasses/all-spectacle-frames.html?facefit=medium&color=pink]
    - For LENSES page: [ACTION:NAVIGATE:/lenses]
    - When recommending frames, ALWAYS end with "Would you like me to take you there?" and include the navigate action with color + facefit filters based on the customer's request and measurements.
{catalog_section}
{lens_section}
{lens_avail_section}
{frame_section}
{faq_section}
{customer_measurements_section}
21. Shipping is Free worldwide on all orders with no minumum. Never quote a shipping cost or fees.
22. NEVER fabricate page URLs. The ONLY valid navigable pages are:
    - /eyeglasses/all-spectacle-frames.html (browse frames)
    - /lenses (lens types)
    - /tryon (face measurement)
    - /checkout (cart/checkout)
    - /favorites (your saved favorites)
    - /profile (customer profile)
    If a customer asks for a page that doesn't exist  tell them to contact support or create a ticket.
23. ACTION INTEGRITY: Never claim you have opened, navigated to, or taken the customer somewhere unless you ALSO include the matching [ACTION:NAVIGATE:...] tag in that same reply. If you are only offering to navigate, ask the yes/no question WITHOUT claiming it is already done. Do not say "Let me take you there" or "I've opened it" on a turn that has no [ACTION:NAVIGATE:...] tag.
"""
    return prompt


def _get_conversation_history(db, session_id, limit=20):
    """Get recent messages for context."""
    cur = db.cursor()
    cur.execute(
        """SELECT role, content FROM chat_messages
           WHERE session_id = %s AND source IN ('customer', 'ai')
           AND status != 'failed'
           ORDER BY created_at DESC LIMIT %s""",
        (session_id, limit)
    )
    rows = list(cur.fetchall())
    rows.reverse()
    return [{'role': r['role'], 'content': r['content']} for r in rows]


def _sanitize_tool_call_message(message):
    """Convert a raw SDK assistant tool-call message into a minimal dict with
    only role/content/tool_calls, dropping provider-specific fields (e.g.
    reasoning_content) that DeepSeek can reject on the follow-up request."""
    tcs = []
    for tc in (getattr(message, 'tool_calls', None) or []):
        tcs.append({
            'id': tc.id,
            'type': 'function',
            'function': {'name': tc.function.name, 'arguments': tc.function.arguments},
        })
    return {'role': 'assistant', 'content': getattr(message, 'content', None) or '', 'tool_calls': tcs}


def _call_deepseek_wrapped(messages, is_india, endpoint, gate_key):
    """Wrapper-routed variant of the tool-calling flow. Uses the AI capacity/
    deadline wrapper (non-thinking deepseek-v4-flash). On a model-layer shed
    (capacity/deadline/provider) returns (None, 'ai_temporarily_unavailable')
    rather than silently bypassing the cap via the legacy direct-SDK fallback."""
    from flask import g, has_request_context
    from .ai_client import call_model, ModelError
    rid = getattr(g, "request_id", "-") if has_request_context() else "-"
    msgs = list(messages)
    try:
        resp = call_model(
            workload="deepseek_chat", messages=msgs, max_tokens=800, temperature=0.7,
            endpoint=endpoint, request_id=rid,
            tools=SEARCH_PRODUCTS_TOOL, tool_choice="auto",
        )
        choice = resp.choices[0]
        if choice.finish_reason == 'tool_calls' and choice.message.tool_calls:
            tool_call = choice.message.tool_calls[0]
            if tool_call.function.name == 'search_products':
                try:
                    args = json.loads(tool_call.function.arguments)
                except Exception:
                    args = {}
                results = _search_catalog(
                    color=args.get('color'), shape=args.get('shape'),
                    facefit=args.get('facefit'), min_price=args.get('min_price'),
                    max_price=args.get('max_price'), keyword=args.get('keyword'),
                    is_india=is_india, limit=15,
                )
                result_text = json.dumps(results, ensure_ascii=False) if results \
                    else "No products found matching those filters. Try broader criteria."
                try:
                    from flask import g as _g, has_request_context as _hrc
                    if _hrc():
                        _g._chat_nav_products = results
                        _g._chat_nav_filters = _nav_filters_from_args(args)
                except Exception:
                    pass
                # Re-send a sanitized assistant tool-call message (only role/
                # content/tool_calls) — the raw SDK object can carry provider
                # fields (e.g. reasoning_content) that DeepSeek intermittently
                # 400s on for the follow-up turn.
                msgs.append(_sanitize_tool_call_message(choice.message))
                msgs.append({'role': 'tool', 'tool_call_id': tool_call.id, 'content': result_text})
                try:
                    resp2 = call_model(
                        workload="deepseek_chat", messages=msgs, max_tokens=800,
                        temperature=0.7, endpoint=endpoint, request_id=rid,
                    )
                    reply2 = (resp2.choices[0].message.content or "").strip()
                    if reply2:
                        return reply2, None
                except ModelError:
                    return None, 'ai_temporarily_unavailable'
                except Exception:
                    pass
                # Fallback: provider rejected the tool-result turn or returned
                # empty. Retry once as a plain completion with the search
                # results injected as context (no tool_call structure).
                fb = list(messages)
                fb.append({'role': 'system',
                           'content': 'Matching products (recommend ONLY from these; '
                                      'do not invent any):\n' + result_text})
                resp3 = call_model(
                    workload="deepseek_chat", messages=fb, max_tokens=800,
                    temperature=0.7, endpoint=endpoint, request_id=rid,
                )
                return (resp3.choices[0].message.content or "").strip(), None
        return (choice.message.content or "").strip(), None
    except ModelError:
        return None, 'ai_temporarily_unavailable'
    except Exception as e:
        return None, str(e)


def _call_deepseek(system_prompt, history, user_message, is_india=False,
                   endpoint="chat_gateway.chat", gate_key=None):
    """Call LLM API with tool-calling support for product search.

    Flow:
    1. Send message with search_products tool available
    2. If LLM calls search_products → execute search → send results back
    3. LLM generates final reply using search results
    """
    api_key = _config.get('llm_api_key') or _config.get('deepseek_api_key')
    if not api_key:
        return None, 'No API key configured'

    base_url = _config.get('llm_base_url', 'https://api.deepseek.com')
    model = _config.get('deepseek_chat_model') or _config.get('llm_model', 'deepseek-chat')
    _eb = {"thinking": {"type": "disabled"}} if _config.get('deepseek_thinking', 'disabled') == 'disabled' else {}

    messages = [{'role': 'system', 'content': system_prompt}]
    for msg in history[-16:]:
        messages.append({'role': msg['role'], 'content': msg['content']})
    messages.append({'role': 'user', 'content': user_message})

    # Route via the wrapper only when the gate is on for this endpoint label.
    # The decision is path-selection only; it does not alter prompts, model,
    # response formatting, idempotency, DB writes, retries, or error contracts.
    _bucket = -1
    _pct = 0
    try:
        from .ai_client import wrapper_route, log_route
        _enabled, _bucket, _pct = wrapper_route(gate_key or endpoint, endpoint=endpoint)
        if _enabled:
            log_route(endpoint, "wrapper", _bucket, _pct)
            return _call_deepseek_wrapped(messages, is_india, endpoint, gate_key)
    except Exception:
        pass

    def _run_direct():
        try:
            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=15.0
            )

            # First call: with tools available
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=800,
                temperature=0.7,
                tools=SEARCH_PRODUCTS_TOOL,
                tool_choice="auto",
                extra_body=_eb
            )

            choice = response.choices[0]

            # Check if the model wants to call a tool
            if choice.finish_reason == 'tool_calls' and choice.message.tool_calls:
                tool_call = choice.message.tool_calls[0]
                if tool_call.function.name == 'search_products':
                    # Parse arguments
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except Exception:
                        args = {}

                    # Execute search
                    results = _search_catalog(
                        color=args.get('color'),
                        shape=args.get('shape'),
                        facefit=args.get('facefit'),
                        min_price=args.get('min_price'),
                        max_price=args.get('max_price'),
                        keyword=args.get('keyword'),
                        is_india=is_india,
                        limit=15
                    )

                    # Format results for the model
                    if results:
                        result_text = json.dumps(results, ensure_ascii=False)
                    else:
                        result_text = "No products found matching those filters. Try broader criteria."

                    # Capture matched products so navigation can use the
                    # canonical catalog URL deterministically (see _resolve_product_nav).
                    try:
                        from flask import g as _g, has_request_context as _hrc
                        if _hrc():
                            _g._chat_nav_products = results
                            _g._chat_nav_filters = _nav_filters_from_args(args)
                    except Exception:
                        pass
                    # Add tool call and result to messages
                    messages.append(choice.message)
                    messages.append({
                        'role': 'tool',
                        'tool_call_id': tool_call.id,
                        'content': result_text
                    })

                    # Second call: generate final reply with search results
                    response2 = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        max_tokens=800,
                        temperature=0.7,
                        extra_body=_eb
                    )
                    reply = response2.choices[0].message.content.strip()
                    return reply, None

            # No tool call — direct reply
            reply = choice.message.content.strip()
            return reply, None
        except Exception as e:
            # Fallback: try without tools (in case provider doesn't support them)
            try:
                client = OpenAI(api_key=api_key, base_url=base_url, timeout=15.0)
                response = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=800,
                    temperature=0.7,
                    extra_body=_eb
                )
                reply = response.choices[0].message.content.strip()
                return reply, None
            except Exception as e2:
                return None, str(e2)

    _t0 = time.monotonic()
    reply, err = _run_direct()
    try:
        from .ai_client import log_route
        log_route(
            endpoint, "direct", _bucket, _pct,
            outcome=("ok" if err is None else "error"),
            total_duration_ms=int((time.monotonic() - _t0) * 1000),
            content_empty=(1 if (err is None and not (reply or "").strip()) else 0),
        )
    except Exception:
        pass
    return reply, err





# ─── Ticket Notifications (Email / WhatsApp / SMS) ───
SITE_SENDER_EMAIL = {
    'in.optiwar.com': 'indiasupport@optiwar.com',
    'optiwar.in': 'indiasupport@optiwar.com',
    'optiwar.com': 'support@optiwar.com',
    'eu.lensbazaar.com': 'support@eu.lensbazaar.com',
    'lensbazaar.com': 'support@lensbazaar.com',
}
ADMIN_EMAIL = 'admin@optiwar.com'


def _get_sender_email(page_url):
    """Return site-specific sender email based on page_url."""
    url = (page_url or '').lower()
    for domain, email in sorted(SITE_SENDER_EMAIL.items(), key=lambda x: -len(x[0])):
        if domain in url:
            return email
    return 'support@optiwar.com'


def _send_ticket_email(customer_email, customer_name, ticket_id, summary, page_url):
    """Send ticket confirmation email to customer and admin.
    
    IMPORTANT: Do NOT change the subject format — replies thread by subject line.
    """
    smtp_host = os.environ.get('SMTP_HOST', 'mail.ket.ltd')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USERNAME', '')
    smtp_pass = os.environ.get('SMTP_PASSWORD', '')

    if not smtp_user or not smtp_pass:
        current_app.logger.warning("[Ticket Email] SMTP credentials not configured — skipping email")
        return False

    sender = _get_sender_email(page_url)
    subject = f"[{ticket_id}] Your Support Ticket Has Been Created"

    # Email body to customer
    customer_body = f"""Dear {customer_name},

Your support ticket {ticket_id} has been created successfully.

Summary of your concern:
{summary}

Our team will review and get back to you shortly.

IMPORTANT: Please do NOT change the subject line when replying to this email. This ensures your reply is linked to your ticket automatically.

To add more information or follow up, simply reply to this email.

Best regards,
Optiwar Support Team
"""

    # Email body to admin
    admin_body = f"""New support ticket created:

Ticket ID: {ticket_id}
Customer: {customer_name} ({customer_email})
Source: {page_url or 'unknown'}

Chat Summary:
{summary}

---
View full conversation at support.ket.ltd.
"""

    try:
        # Send to customer
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = customer_email
        msg['Reply-To'] = ADMIN_EMAIL
        msg['Date'] = formatdate(localtime=True)
        msg.set_content(customer_body)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        current_app.logger.info(f"[Ticket Email] Sent to customer {customer_email} for {ticket_id}")

        # Send to admin
        admin_msg = EmailMessage()
        admin_msg['Subject'] = subject
        admin_msg['From'] = sender
        admin_msg['To'] = ADMIN_EMAIL
        admin_msg['Reply-To'] = customer_email
        admin_msg['Date'] = formatdate(localtime=True)
        admin_msg.set_content(admin_body)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(admin_msg)

        current_app.logger.info(f"[Ticket Email] Sent to admin {ADMIN_EMAIL} for {ticket_id}")
        return True

    except Exception as e:
        current_app.logger.error(f"[Ticket Email] Failed to send for {ticket_id}: {e}")
        return False


def _send_fallback_email(name, email, local_ticket_id, ket_ticket_id, summary, transcript, page_url):
    """Always send ticket notification to admin@optiwar.com as secondary fallback."""
    smtp_host = os.environ.get('SMTP_HOST', 'mail.ket.ltd')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USERNAME', '')
    smtp_pass = os.environ.get('SMTP_PASSWORD', '')

    if not smtp_user or not smtp_pass:
        current_app.logger.warning("[Fallback Email] SMTP credentials not configured — skipping")
        return False

    sender = _get_sender_email(page_url)
    subject = f"[AI Chat Ticket] Local #{local_ticket_id} | KET: {ket_ticket_id or 'FAILED'}"

    body = f"""AI Chat Ticket Created:

Local Ticket ID: {local_ticket_id or 'DB INSERT FAILED'}
KET Ticket ID: {ket_ticket_id or 'KET FORWARDING FAILED'}
Customer: {name} ({email})
Source: {page_url or 'unknown'}

Summary:
{summary}

Full Chat Transcript:
{transcript}
"""
    try:
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = ADMIN_EMAIL
        msg['Date'] = formatdate(localtime=True)
        msg.set_content(body)

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        current_app.logger.info(f"[Fallback Email] Sent to {ADMIN_EMAIL} for local #{local_ticket_id}")
        return True
    except Exception as e:
        current_app.logger.error(f"[Fallback Email] Failed: {e}")
        return False


def _forward_ticket_from_chat(db, session_id, session, page_url):
    """
    Create ticket from AI chat — mirrors contact form flow.
    1. Generate summary from chat transcript
    2. Insert into local `tickets` DB table
    3. Forward to KET support.ket.ltd API (with transcript)
    4. Fallback email to admin@optiwar.com (always)
    5. Log everything
    Returns: (local_ticket_id, ket_ticket_id)
    """
    # Get chat transcript
    history = _get_conversation_history(db, session_id, limit=50)
    transcript_lines = []
    for msg in history:
        role = "Customer" if msg['role'] == 'user' else "AI"
        transcript_lines.append(f"{role}: {msg['content']}")
    transcript = "\n".join(transcript_lines)

    # Generate AI summary
    summary = _generate_chat_summary(db, session_id)

    contact_name = session.get('contact_name') or 'Visitor'
    contact_email = session.get('contact_email') or ''

    # STEP 1: Insert into local DB (same as contact form)
    local_ticket_id = None
    try:
        local_ticket_id = create_ticket_in_db(
            name=contact_name,
            email=contact_email,
            subject=f"[AI Chat] {summary[:100]}",
            message=f"[AI-Assisted Ticket]\n\nSummary:\n{summary}\n\nFull Transcript:\n{transcript}"
        )
        current_app.logger.info(f"[Chat Ticket] Local DB ticket #{local_ticket_id} created")
    except Exception as e:
        current_app.logger.error(f"[Chat Ticket] DB insert failed: {e}")

    # STEP 2: Forward to KET (with transcript + session_id)
    ket_ticket_id = None
    try:
        from .crm import _forward_to_ket
        ket_ticket_id = _forward_to_ket(
            name=contact_name,
            email=contact_email,
            phone='',
            subject=f"[AI Chat] {summary[:100]}",
            description=f"[AI-Assisted Ticket]\n\n{summary}",
            source="ai_chat_handover",
            chat_transcript=json.dumps([{'role': m['role'], 'content': m['content']} for m in history]),
            session_id=session_id
        )
        if ket_ticket_id:
            current_app.logger.info(f"[Chat Ticket] KET ticket {ket_ticket_id} created")
        else:
            current_app.logger.warning("[Chat Ticket] KET forwarding returned None")
    except Exception as e:
        current_app.logger.error(f"[Chat Ticket] KET forwarding failed: {e}")

    # STEP 3: Fallback email to admin@optiwar.com (ALWAYS fires)
    try:
        _send_fallback_email(
            contact_name, contact_email,
            local_ticket_id, ket_ticket_id,
            summary, transcript, page_url
        )
    except Exception as e:
        current_app.logger.error(f"[Chat Ticket] Fallback email failed: {e}")

    return local_ticket_id, ket_ticket_id


def _send_ticket_whatsapp(customer_phone, customer_name, ticket_id, summary):
    """Send ticket notification via WhatsApp.
    
    TODO: Configure WhatsApp Business API integration.
    Required env vars:
        WHATSAPP_API_URL - WhatsApp Business API endpoint
        WHATSAPP_API_TOKEN - Authentication token
        WHATSAPP_TEMPLATE_ID - Pre-approved message template ID
    """
    wa_url = os.environ.get('WHATSAPP_API_URL', '')
    wa_token = os.environ.get('WHATSAPP_API_TOKEN', '')
    wa_template = os.environ.get('WHATSAPP_TEMPLATE_ID', '')

    if not wa_url or not wa_token:
        # WhatsApp not configured yet — skip silently
        return False

    # TODO: Implement WhatsApp API call when credentials are provided
    # Example payload for WhatsApp Business API:
    # {
    #   "messaging_product": "whatsapp",
    #   "to": customer_phone,
    #   "type": "template",
    #   "template": {
    #       "name": wa_template,
    #       "language": {"code": "en"},
    #       "components": [{"type": "body", "parameters": [
    #           {"type": "text", "text": customer_name},
    #           {"type": "text", "text": ticket_id},
    #           {"type": "text", "text": summary[:100]}
    #       ]}]
    #   }
    # }
    current_app.logger.info(f"[Ticket WhatsApp] Would send to {customer_phone} for {ticket_id} (not configured)")
    return False


def _send_ticket_sms(customer_phone, customer_name, ticket_id):
    """Send ticket notification via SMS.
    
    TODO: Configure SMS gateway integration.
    Required env vars:
        SMS_API_URL - SMS gateway API endpoint
        SMS_API_KEY - Authentication key
        SMS_SENDER_ID - Registered sender ID
    """
    sms_url = os.environ.get('SMS_API_URL', '')
    sms_key = os.environ.get('SMS_API_KEY', '')

    if not sms_url or not sms_key:
        # SMS not configured yet — skip silently
        return False

    # TODO: Implement SMS API call when credentials are provided
    # Message: f"Your Optiwar support ticket {ticket_id} has been created. Our team will get back to you shortly."
    current_app.logger.info(f"[Ticket SMS] Would send to {customer_phone} for {ticket_id} (not configured)")
    return False


# ─── Ticket Prefix by Site ───
SITE_TICKET_PREFIX = {
    'in.optiwar.com': 'IN-OW',
    'optiwar.in': 'IN-OW',
    'optiwar.com': 'OW',
    'eu.lensbazaar.com': 'EU-LB',
    'lensbazaar.com': 'LB',
    'ket.com': 'KET',
}

def _get_ticket_prefix(page_url):
    """Return site-specific ticket prefix based on page_url domain."""
    url = (page_url or '').lower()
    # Check more specific domains first (in.optiwar.com before optiwar.com)
    for domain, prefix in sorted(SITE_TICKET_PREFIX.items(), key=lambda x: -len(x[0])):
        if domain in url:
            return prefix
    return 'OW'  # default



def _generate_chat_summary(db, session_id):
    """Generate a brief summary of the chat conversation for the support team."""
    history = _get_conversation_history(db, session_id, limit=30)
    if not history:
        return "No conversation history available."

    # Build transcript for summarization
    transcript_lines = []
    for msg in history:
        role_label = "Customer" if msg['role'] == 'user' else "AI"
        transcript_lines.append(f"{role_label}: {msg['content']}")
    transcript = "\n".join(transcript_lines)

    summary_prompt = """You are a support ticket summarizer. Read the chat transcript below and produce a brief internal note (3-5 bullet points max) covering:
- Customer name and what they asked about
- Key issue or request
- What the AI already told them
- Any unresolved concern or reason for escalation

Keep it concise — this is for the support team picking up the ticket. Do NOT include greetings or filler."""

    summary, error = _call_deepseek(summary_prompt, [], f"TRANSCRIPT:\n{transcript}",
                                    endpoint="chat_gateway.summary", gate_key=session_id)
    if error or not summary:
        # Fallback: just list last few messages
        fallback_lines = []
        for msg in history[-5:]:
            role_label = "Customer" if msg['role'] == 'user' else "AI"
            fallback_lines.append(f"- {role_label}: {msg['content'][:100]}")
        return "**Chat Summary (auto-generated):**\n" + "\n".join(fallback_lines)

    return f"**Chat Summary (auto-generated):**\n{summary}"


def _clean_ai_reply(reply):
    """Remove action tags, return (cleaned_reply, actions)."""
    actions = []
    navigate_url = None
    if '[ACTION:HUMAN_HANDOVER]' in reply:
        actions.append('human_handover')
        reply = reply.replace('[ACTION:HUMAN_HANDOVER]', '').strip()
    if '[ACTION:CREATE_TICKET]' in reply:
        actions.append('create_ticket')
        reply = reply.replace('[ACTION:CREATE_TICKET]', '').strip()
    # Extract NAVIGATE action with URL
    nav_match = re.search(r'\[ACTION:NAVIGATE:([^\]]+)\]', reply)
    if nav_match:
        actions.append('navigate')
        navigate_url = nav_match.group(1).strip()
        reply = re.sub(r'\[ACTION:NAVIGATE:[^\]]+\]', '', reply).strip()
    reply = re.sub(r'\[PRODUCTS:[\d,]+\]', '', reply).strip()
    return reply, actions, navigate_url


def _resolve_product_nav(navigate_url, user_message):
    """Deterministically point navigation at the matched product's canonical
    catalog URL instead of the LLM's freelanced link.

    The model was told to emit the exact catalog ``url`` but repeatedly
    fabricated ``/product/<id>`` (404) or browse pages. When a product search
    matched, the server already knows the real URL, so we use it directly.

    Rules (only affects WHERE navigation goes, never adds navigation for a
    generic browse request):
    - a link that already looks canonical (contains ``pid=``) is kept as-is;
    - if the customer named a specific product code that matched, go to it;
    - else if exactly one product matched, go to it;
    - otherwise leave the model's navigate_url untouched.
    """
    try:
        from flask import g as _g, has_request_context as _hrc
        products = getattr(_g, "_chat_nav_products", None) if _hrc() else None
    except Exception:
        products = None

    # Model already produced a valid product link — trust it.
    if navigate_url and "pid=" in navigate_url:
        return navigate_url

    um = user_message or ""
    wants_go = bool(re.search(
        r"take me|take you there|go to|bring me|navigate|open |product page|show me the",
        um, re.IGNORECASE))

    chosen = None
    # among products the model searched this turn, prefer a code the customer
    # named, else a single unambiguous match.
    if products:
        for p in products:
            code = (p.get("code") or "").strip()
            if code and re.search(r"\b" + re.escape(code) + r"\b", um, re.IGNORECASE):
                chosen = p
                break
        if chosen is None and len(products) == 1:
            chosen = products[0]

    # Fallback: the model may not have called the search tool at all. If the
    # customer named a product code and wants to be taken there, look it up
    # directly in the catalog so navigation still works deterministically.
    if chosen is None and (wants_go or navigate_url):
        for tok in re.findall(r"\b[A-Za-z]{1,3}\d{1,4}\b", um):
            try:
                hits = _search_catalog(keyword=tok, limit=5)
            except Exception:
                hits = []
            for h in hits:
                if (h.get("code") or "").strip().upper() == tok.upper():
                    chosen = h
                    break
            if chosen:
                break

    if not chosen:
        return navigate_url
    url = (chosen.get("url") or "").strip()
    if not url:
        return navigate_url
    if navigate_url or wants_go:
        return url
    return navigate_url


def _nav_filters_from_args(args):
    """Pick the catalog filters the model actually used, so a multi-product
    recommendation can navigate to those *filtered* results rather than a
    generic catalogue (preserves recommendation identity)."""
    out = {}
    for k in acr.NAV_FILTER_KEYS:
        v = (args or {}).get(k)
        if v is not None and str(v).strip() != '':
            out[k] = str(v).strip()
    return out


def _recover_nav_target():
    """Best-effort navigation target for a recommendation turn, so a later
    confirmation ("yes") always resolves to a real, non-dead destination that
    preserves the recommendation's identity.

    - exactly one product searched -> that product's canonical URL
    - several products searched     -> the frames listing filtered by the same
                                       criteria the model used (not a generic
                                       catalogue); generic listing only if no
                                       filters are known
    - nothing searched              -> None
    """
    try:
        from flask import g as _g, has_request_context as _hrc
        in_ctx = _hrc()
        products = getattr(_g, "_chat_nav_products", None) if in_ctx else None
        filters = getattr(_g, "_chat_nav_filters", None) if in_ctx else None
    except Exception:
        products, filters = None, None
    if not products:
        return None
    if len(products) == 1:
        url = (products[0].get("url") or "").strip()
        if url:
            return url
    return acr.filtered_listing_url(filters)


# ─── API Endpoints ───

def _chat_cookie_serializer():
    return URLSafeSerializer(current_app.config.get("SECRET_KEY", ""), salt="ow-chat-session")


def _set_chat_owner_cookie(resp, session_id):
    """Bind this browser to its chat session via a signed HttpOnly cookie."""
    try:
        token = _chat_cookie_serializer().dumps(session_id)
        resp.set_cookie("ow_chat_token", token, max_age=7 * 24 * 3600,
                        httponly=True, secure=True, samesite="Lax")
    except Exception as e:
        current_app.logger.warning(f"[chat] could not set owner cookie: {e}")
    return resp


def _is_chat_owner(session_id):
    """True only if the request carries a valid signed cookie for this session."""
    token = request.cookies.get("ow_chat_token", "")
    if not token:
        return False
    try:
        return _chat_cookie_serializer().loads(token) == session_id
    except (BadSignature, Exception):
        return False


def _acr_enabled_for(contact_email):
    """ACR customer-facing gate (safeguard #3, limited canary).

    - ``ACR_ACTIONS_ENABLED`` false  -> off for everyone (legacy stable path).
    - ``ACR_CANARY_ONLY`` true (default) -> on only for approved canary sessions:
      a signed ``ow_acr_canary`` cookie, or a customer whose email is in the
      ``ACR_CANARY_EMAILS`` allow-list.
    - ``ACR_CANARY_ONLY`` false -> on for all sessions (post-canary rollout).

    Fail-safe: any error resolves to False (legacy path), never a crash.
    """
    try:
        cookie_ok = False
        raw = request.cookies.get('ow_acr_canary', '')
        if raw:
            try:
                cookie_ok = URLSafeSerializer(
                    current_app.config['SECRET_KEY'], salt='acr-canary').loads(raw) == 'on'
            except (BadSignature, Exception):
                cookie_ok = False
        return acr.canary_allows(
            current_app.config.get('ACR_ACTIONS_ENABLED', False),
            current_app.config.get('ACR_CANARY_ONLY', True),
            cookie_ok,
            contact_email,
            current_app.config.get('ACR_CANARY_EMAILS', ''),
        )
    except Exception:
        return False


@bp.route('/admin/acr-canary', methods=['GET', 'POST'])
def acr_canary_toggle():
    """Admin-only: set/clear the signed ACR canary opt-in cookie for this browser.

    ``?on=1`` (default) enrols this browser in the canary; ``?on=0`` removes it.
    Gated by the shared /ops auth (admin session or Bearer OPS_API_TOKEN)."""
    from .ops import _require_ops_auth
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    on = str(request.args.get('on', '1')).lower() not in ('0', 'false', 'no', '')
    resp = make_response(jsonify({'canary': on}))
    if on:
        tok = URLSafeSerializer(current_app.config['SECRET_KEY'],
                                salt='acr-canary').dumps('on')
        resp.set_cookie('ow_acr_canary', tok, max_age=7 * 24 * 3600, path='/',
                        secure=True, httponly=True, samesite='Lax')
    else:
        # Mirror the set_cookie attributes on deletion so opt-out reliably
        # clears the cookie even under strict attribute-parity cookie policies.
        resp.delete_cookie('ow_acr_canary', path='/',
                           secure=True, httponly=True, samesite='Lax')
    return resp


@bp.route('/start', methods=['POST'])
def chat_start():
    """Start a new chat session or resume active one."""
    data = request.get_json(force=True, silent=True) or {}
    email = data.get('email', '').strip()
    name = data.get('name', '').strip()
    page_url = data.get('page_url', '')
    customer_id = data.get('customer_id')

    if not email:
        return jsonify({'error': 'email required'}), 400

    db = _get_db()
    cur = db.cursor()

    # Check for existing active session
    cur.execute(
        """SELECT session_id, status, created_at FROM chat_sessions
           WHERE contact_email = %s AND status IN ('active', 'ai_pending')
           ORDER BY last_activity DESC LIMIT 1""",
        (email,)
    )
    existing = cur.fetchone()

    if existing:
        session_id = existing['session_id']
        # Update last activity and page
        cur.execute(
            """UPDATE chat_sessions SET last_activity = NOW(), current_page_url = %s
               WHERE session_id = %s""",
            (page_url, session_id)
        )
        _log_event(db, session_id, 'session_resumed', {'page_url': page_url})
        resp = make_response(jsonify({
            'session_id': session_id,
            'status': existing['status'],
            'resumed': True
        }))
        return _set_chat_owner_cookie(resp, session_id)

    # Create new session
    session_id = f"chat_{uuid.uuid4().hex[:16]}"
    cur.execute(
        """INSERT INTO chat_sessions
           (session_id, customer_id, contact_email, contact_name, status, current_page_url, created_at, last_activity)
           VALUES (%s, %s, %s, %s, 'active', %s, NOW(), NOW())""",
        (session_id, customer_id, email, name, page_url)
    )
    _log_event(db, session_id, 'session_created', {
        'email': email, 'name': name, 'page_url': page_url
    })

    # Send welcome message
    welcome = f"Hi {name or 'there'}, I am here to help \u2013 ask me anything you need"
    _insert_message(db, session_id, 'ai', 'assistant', welcome, status='sent')



    db.close()
    resp = make_response(jsonify({
        'session_id': session_id,
        'status': 'active',
        'resumed': False
    }))
    return _set_chat_owner_cookie(resp, session_id)


@bp.route('/message', methods=['POST'])
def chat_message():
    """Send a customer message and get AI response (inline for Phase 1A)."""
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get('session_id', '').strip()
    content = data.get('content', '').strip()
    page_url = data.get('page_url', '')
    client_message_id = str(data.get('client_message_id', '')).strip()[:64]

    # Fallback: if page_url not provided in message, fetch from session
    if not page_url:
        _db_tmp = _get_db()
        _cur_tmp = _db_tmp.cursor()
        _cur_tmp.execute("SELECT current_page_url FROM chat_sessions WHERE session_id = %s", (session_id,))
        _row_tmp = _cur_tmp.fetchone()
        if _row_tmp and _row_tmp.get('current_page_url'):
            page_url = _row_tmp['current_page_url']
        _db_tmp.close()

    if not session_id or not content:
        return jsonify({'error': 'session_id and content required'}), 400

    db = _get_db()
    cur = db.cursor()

    # Verify session exists and is active
    cur.execute(
        """SELECT session_id, contact_name, contact_email, customer_id, status FROM chat_sessions
           WHERE session_id = %s""",
        (session_id,)
    )
    session = cur.fetchone()
    if not session:
        db.close()
        return jsonify({'error': 'session not found'}), 404
    if session['status'] == 'human_open':
        # Human agent has taken over — store message, AI stays silent
        customer_msg_id = _insert_message(db, session_id, 'customer', 'user', content)
        cur.execute("UPDATE chat_sessions SET last_activity = NOW() WHERE session_id = %s", (session_id,))
        db.close()
        return jsonify({
            'session_id': session_id,
            'reply': 'Your message has been sent to my supervisor. They will respond shortly.',
            'message_id': customer_msg_id,
            'status': 'sent',
            'actions': ['human_active']
        })

    if session['status'] == 'archived':
        db.close()
        return jsonify({
            'session_id': session_id,
            'reply': 'This conversation has been archived. Please start a new chat.',
            'status': 'archived',
            'actions': []
        })

    if session['status'] not in ('active', 'ai_pending'):
        db.close()
        return jsonify({'error': f"session is {session['status']}"}), 409

    # 1. Insert customer message — idempotent via UNIQUE(session_id, source,
    #    client_message_id): a soft-retry (same client_message_id after a 503) or a
    #    concurrent duplicate submit collapses to a single stored row (DB-enforced,
    #    race-safe). metadata keeps the legacy 'cmid' for backward-compatible reads.
    customer_msg_id = _insert_message(
        db, session_id, 'customer', 'user', content,
        metadata={'cmid': client_message_id} if client_message_id else None,
        client_message_id=client_message_id or None)

    # 2. Log ai_started event
    _log_event(db, session_id, 'ai_started', {'user_message': content[:200]})

    # Update session
    cur.execute(
        """UPDATE chat_sessions SET status = 'ai_pending', last_activity = NOW(),
           current_page_url = %s WHERE session_id = %s""",
        (page_url, session_id)
    )

    # 3. Generate AI response (inline for Phase 1A)
    contact_name = session['contact_name'] or 'Visitor'
    is_india = 'in.optiwar.com' in (page_url or '') or 'optiwar.in' in (page_url or '')

    customer_id = session.get('customer_id')
    system_prompt = _build_system_prompt(contact_name, is_india, content, customer_id=customer_id)
    history = _get_conversation_history(db, session_id)
    ai_reply, error = _call_deepseek(system_prompt, history, content, is_india=is_india,
                                     endpoint="chat_gateway.message", gate_key=session_id)

    if error == 'ai_temporarily_unavailable':
        # Retryable capacity/deadline shed from the wrapper. Return the public 503
        # contract so the frontend can soft-retry once (reusing client_message_id).
        # Do NOT store a 'failed' reply — keep the conversation clean for the retry.
        _log_event(db, session_id, 'ai_shed', {'reason': error})
        cur.execute(
            """UPDATE chat_sessions SET status = 'active', last_activity = NOW()
               WHERE session_id = %s""",
            (session_id,)
        )
        db.close()
        from .ai_client import unavailable_contract
        status, body, headers = unavailable_contract(getattr(g, 'request_id', '-'))
        return jsonify(body), status, headers

    if error:
        # Non-retryable AI failure — insert failed message + event
        fail_msg = 'Sorry, I\'m having trouble right now. Please try again.'
        _insert_message(db, session_id, 'ai', 'assistant', fail_msg,
                       status='failed',
                       metadata={'error': error[:500]})
        _log_event(db, session_id, 'ai_failed', {'error': error[:500]})
        cur.execute(
            """UPDATE chat_sessions SET status = 'active', last_activity = NOW()
               WHERE session_id = %s""",
            (session_id,)
        )
        db.close()
        return jsonify({
            'session_id': session_id,
            'reply': fail_msg,
            'status': 'failed'
        })

    # Clean AI reply (handle action tags)
    ai_reply, actions, navigate_url = _clean_ai_reply(ai_reply)

    # Deterministic product navigation: override the model's freelanced link
    # with the matched product's canonical catalog URL when applicable.
    navigate_url = _resolve_product_nav(navigate_url, content)

    # ── ACR canary gate (safeguard #3) ──
    # When ACR is not enabled for this session, ordinary customers keep the exact
    # pre-ACR stable path (no pending actions, no fallback button, no result
    # reporting). ACR action-integrity runs only for approved canary sessions.
    acr_action = None
    if _acr_enabled_for(session.get('contact_email')):
        # ── ACR A1: resolve a bare confirmation against a live pending action ──
        # If the model produced no navigation this turn but the customer just
        # confirmed ("yes"/"take me there"), honour the action we proposed earlier
        # instead of re-inferring from the word "yes" (the silent-failure bug).
        # A confirmation is only resolved against a pending NAVIGATE when this turn
        # is NOT itself a supervisor handover / ticket confirmation — otherwise a
        # "yes" answering "connect you to my supervisor? Yes or No" would be
        # hijacked into a stale redirect.
        _confirm_is_navigational = not ({'human_handover', 'create_ticket'} & set(actions))
        if not navigate_url and _confirm_is_navigational and acr.is_confirmation(content):
            pending = acr.get_live_pending_action(db, session_id, 'NAVIGATE')
            if pending and pending.get('target'):
                navigate_url = pending['target']
                acr_action = {'action_id': pending['action_id'], 'type': 'NAVIGATE',
                              'target': navigate_url}
                acr.mark_action(db, pending['action_id'], 'CONFIRMED')

        if navigate_url and 'navigate' not in actions:
            actions.append('navigate')

        # ── ACR A2: promise-without-action detection ──
        # The model claimed a navigation but emitted no target -> recover one if
        # we can, otherwise log the incident (never leave the promise unbacked).
        if 'navigate' not in actions and acr.promises_navigation(ai_reply):
            recovered = _recover_nav_target()
            acr.log_event(db, 'AI_PROMISE_WITHOUT_ACTION', session_id=session_id,
                          page_url=page_url,
                          payload={'reply_head': ai_reply[:160], 'recovered': bool(recovered)})
            if recovered:
                navigate_url = recovered
                actions.append('navigate')

        # ── ACR A1: create/confirm a structured action + mandatory fallback link ──
        if navigate_url:
            if acr_action is None:
                _aid = acr.create_pending_action(db, session_id, 'NAVIGATE', navigate_url)
                acr.mark_action(db, _aid, 'CONFIRMED')
                acr_action = {'action_id': _aid, 'type': 'NAVIGATE', 'target': navigate_url}
            if not ai_reply.strip():
                ai_reply = 'Opening that for you now.'
            # Always leave a real, clickable fallback so navigation can never
            # silently fail — the reply itself carries the button (survives polling).
            ai_reply = acr.with_fallback_link(ai_reply, navigate_url)
        elif acr.offers_navigation(ai_reply):
            # No navigation this turn, but the assistant *offered* to navigate
            # ("...take you to these frames?"). Seed a pending action so a
            # follow-up "yes" resolves to a real destination. Only seed on a
            # genuine navigation offer — never on a ticket/handover yes/no prompt
            # — so a later confirmation can't be turned into an unexpected redirect.
            _seed = _recover_nav_target()
            if _seed:
                acr.create_pending_action(db, session_id, 'NAVIGATE', _seed)
    else:
        # Pre-ACR stable path (unchanged legacy behaviour for ordinary customers).
        if navigate_url and 'navigate' not in actions:
            actions.append('navigate')
        if 'navigate' in actions and not ai_reply.strip():
            ai_reply = 'Taking you there now...'

    # Insert AI response — idempotent on the same client_message_id so a concurrent
    # duplicate submit can never store two AI replies for one customer turn. On a
    # duplicate the winning row id is returned; re-read it so both callers return the
    # single stored reply.
    ai_msg_id = _insert_message(db, session_id, 'ai', 'assistant', ai_reply, status='sent',
                                metadata={'actions': actions} if actions else None,
                                client_message_id=client_message_id or None)
    if client_message_id:
        cur.execute("SELECT content, metadata FROM chat_messages WHERE id = %s", (ai_msg_id,))
        _stored = cur.fetchone()
        if _stored:
            _sc = _stored['content'] if isinstance(_stored, dict) else _stored[0]
            _sm = _stored['metadata'] if isinstance(_stored, dict) else _stored[1]
            if _sc:
                ai_reply = _sc
            if _sm:
                try:
                    actions = json.loads(_sm).get('actions', actions)
                except (ValueError, TypeError):
                    pass
    _log_event(db, session_id, 'ai_completed', {
        'reply_length': len(ai_reply),
        'actions': actions
    })

    # --- Ticket Creation ---
    if 'human_handover' in actions or 'create_ticket' in actions:
        # STEP 1: Create ticket via solid 3-step flow (DB + KET + fallback email)
        local_ticket_id, ket_ticket_id = _forward_ticket_from_chat(db, session_id, session, page_url)

        # Build ticket reference for customer
        ticket_ref = ""
        if ket_ticket_id:
            ticket_ref = ket_ticket_id
        elif local_ticket_id:
            ticket_ref = f"#{local_ticket_id}"

        if 'human_handover' in actions:
            ticket_msg = f"\n\nNow my supervisor will take over further answers. Ticket {ticket_ref}."
            ai_reply += ticket_msg
        else:
            ticket_msg = f"Your support ticket {ticket_ref} has been created. Our team will review and get back to you shortly."
            if not ai_reply.strip():
                ai_reply = ticket_msg
            else:
                ai_reply += f"\n\n{ticket_msg}"

        # Update stored message with ticket reference
        cur.execute(
            "UPDATE chat_messages SET content = %s WHERE id = %s",
            (ai_reply, ai_msg_id)
        )

        # Send ticket confirmation email to customer
        _send_ticket_email(
            customer_email=session['contact_email'],
            customer_name=session['contact_name'] or 'Customer',
            ticket_id=ticket_ref,
            summary=_generate_chat_summary(db, session_id) if '_summary' not in dir() else _summary,
            page_url=page_url
        )

        # WhatsApp/SMS hookpoints (will activate when configured)
        _send_ticket_whatsapp(None, session['contact_name'] or 'Customer', ticket_ref, '')
        _send_ticket_sms(None, session['contact_name'] or 'Customer', ticket_ref)
    # --- End Ticket Creation ---

    # Update session status — AI keeps chatting even after handover
    # human_open is only set when a real agent replies via /agent-reply
    new_status = 'active'
    cur.execute(
        """UPDATE chat_sessions SET status = %s, last_activity = NOW()
           WHERE session_id = %s""",
        (new_status, session_id)
    )

    db.close()
    resp = {
        'session_id': session_id,
        'reply': ai_reply,
        'message_id': ai_msg_id,
        'status': 'sent',
        'actions': actions
    }
    if navigate_url:
        resp['navigate_url'] = navigate_url
    if acr_action:
        resp['action'] = acr_action
    return jsonify(resp)


@bp.route('/messages/<session_id>', methods=['GET'])
def chat_messages(session_id):
    """Get messages for a session (polling endpoint).
    Optional query params: since (ISO timestamp), limit (int).
    Owner-gated: only the browser that started the session (valid signed cookie) may read it.
    """
    if not _is_chat_owner(session_id):
        return jsonify({'error': 'forbidden'}), 403
    since = request.args.get('since', '')
    limit = min(int(request.args.get('limit', 50)), 100)

    db = _get_db()
    cur = db.cursor()

    # Verify session
    cur.execute("SELECT session_id, status FROM chat_sessions WHERE session_id = %s", (session_id,))
    session = cur.fetchone()
    if not session:
        db.close()
        return jsonify({'error': 'session not found'}), 404

    if since:
        cur.execute(
            """SELECT id, source, role, content, status, metadata, created_at
               FROM chat_messages WHERE session_id = %s AND created_at > %s
               ORDER BY created_at ASC LIMIT %s""",
            (session_id, since, limit)
        )
    else:
        cur.execute(
            """SELECT id, source, role, content, status, metadata, created_at
               FROM chat_messages WHERE session_id = %s
               ORDER BY created_at ASC LIMIT %s""",
            (session_id, limit)
        )

    messages = cur.fetchall()
    db.close()

    # Serialize
    result = []
    for m in messages:
        result.append({
            'id': m['id'],
            'source': m['source'],
            'role': m['role'],
            'content': m['content'],
            'status': m['status'],
            'created_at': m['created_at'].isoformat() if m['created_at'] else None,
        })

    return jsonify({
        'session_id': session_id,
        'session_status': session['status'],
        'messages': result,
        'count': len(result)
    })


@bp.route('/action-result', methods=['POST'])
def chat_action_result():
    """ACR A1: the widget reports whether a structured action executed.

    Owner-gated. Body: {session_id, action_id, success, failure_code?, duration_ms?}.
    Also accepts navigator.sendBeacon (text/plain body) fired during page unload.
    """
    data = request.get_json(force=True, silent=True)
    if data is None:
        try:
            data = json.loads(request.get_data(as_text=True) or '{}')
        except (ValueError, TypeError):
            data = {}
    session_id = str(data.get('session_id', '')).strip()
    action_id = str(data.get('action_id', '')).strip()
    if not session_id or not action_id:
        return jsonify({'error': 'session_id and action_id required'}), 400
    if not _is_chat_owner(session_id):
        return jsonify({'error': 'forbidden'}), 403
    success = bool(data.get('success', True))
    failure_code = (str(data.get('failure_code')) if data.get('failure_code') else None)
    try:
        duration_ms = int(data.get('duration_ms')) if data.get('duration_ms') is not None else None
    except (TypeError, ValueError):
        duration_ms = None

    db = _get_db()
    ok = acr.record_action_result(db, action_id, success, failure_code, duration_ms)
    db.close()
    return jsonify({'recorded': ok}), (200 if ok else 404)


@bp.route('/admin/ops-console', methods=['GET'])
def acr_ops_console():
    """ACR A3: read-only AI Operations Console (admin only). ?format=json for data."""
    from flask import session
    from .ops import _require_ops_auth
    if not _require_ops_auth():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        hours = max(1, min(int(request.args.get('hours', 24)), 720))
    except (TypeError, ValueError):
        hours = 24
    try:
        limit = max(1, min(int(request.args.get('limit', 50)), 200))
    except (TypeError, ValueError):
        limit = 50
    db = _get_db()
    try:
        # Audit every access to the PII-bearing console (who / when / IP / scope).
        # NOW() supplies the timestamp; best-effort so it never blocks the view.
        actor = session.get('user_email') or 'bearer_token'
        fwd = request.headers.get('X-Forwarded-For', '')
        client_ip = (fwd.split(',')[0].strip() if fwd else request.remote_addr)
        acr.log_event(db, 'OPS_CONSOLE_ACCESS',
                      payload={'actor': actor, 'ip': client_ip,
                               'scope': {'hours': hours, 'limit': limit},
                               'format': request.args.get('format', 'html')})
        sessions = acr.ops_console_snapshot(db, limit=limit)
        stats = acr.ops_console_stats(db, hours=hours)
    finally:
        db.close()
    # Serialize datetimes for JSON / template safety.
    for s in sessions:
        if s.get('last_activity'):
            s['last_activity'] = s['last_activity'].isoformat()
        la = s.get('last_action')
        if la and la.get('created_at'):
            la['created_at'] = la['created_at'].isoformat()
    if request.args.get('format') == 'json':
        return jsonify({'sessions': sessions, 'stats': stats, 'hours': hours})
    return render_template('acr_ops_console.html', sessions=sessions, stats=stats, hours=hours)


@bp.route('/status', methods=['GET'])
def chat_status():
    """Check session status for a user (used by widget on load)."""
    email = request.args.get('email', '').strip()
    if not email:
        return jsonify({'has_active': False}), 200

    db = _get_db()
    cur = db.cursor()
    cur.execute(
        """SELECT session_id, status, created_at, last_activity FROM chat_sessions
           WHERE contact_email = %s AND status IN ('active', 'ai_pending', 'human_pending', 'human_open')
           ORDER BY last_activity DESC LIMIT 1""",
        (email,)
    )
    active = cur.fetchone()
    db.close()

    if active:
        return jsonify({
            'has_active': True,
            'session_id': active['session_id'],
            'status': active['status'],
            'last_activity': active['last_activity'].isoformat() if active['last_activity'] else None
        })

    return jsonify({'has_active': False})


@bp.route('/resolve', methods=['POST'])
def chat_resolve():
    """Resolve/end a chat session. Accepts calls from widget (no auth) or KET (HMAC auth)."""
    raw_body = request.get_data(as_text=True)
    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get('session_id', '').strip()
    if not session_id:
        return jsonify({'error': 'session_id required'}), 400

    # Either the widget owner (signed cookie) or KET (valid HMAC) may resolve.
    if _verify_ket_signature(
        raw_body,
        request.headers.get('X-KET-Timestamp'),
        request.headers.get('X-KET-Signature'),
    ):
        resolved_by = 'ket'
    elif _is_chat_owner(session_id):
        resolved_by = 'customer'
    else:
        return jsonify({'error': 'forbidden'}), 403

    db = _get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT session_id FROM chat_sessions WHERE session_id = %s",
        (session_id,)
    )
    if not cur.fetchone():
        db.close()
        return jsonify({'error': 'session not found'}), 404
    cur.execute(
        """UPDATE chat_sessions SET status = 'resolved', resolved_at = NOW(), last_activity = NOW()
           WHERE session_id = %s""",
        (session_id,)
    )
    _log_event(db, session_id, 'session_resolved', {'resolved_by': resolved_by})

    # Insert system message
    _insert_message(db, session_id, 'system', 'system',
                   'This conversation has been resolved. Start a new chat anytime!')

    db.close()
    return jsonify({'status': 'resolved', 'session_id': session_id})


@bp.route('/agent-reply', methods=['POST'])
def chat_agent_reply():
    """Receive an agent reply pushed by KET Support (support.ket.ltd).
    HMAC-SHA256 authenticated over "<X-KET-Timestamp>:<raw_body>". Inserts an agent
    message so the polling widget delivers it, and flips the session to human_open."""
    raw_body = request.get_data(as_text=True)
    if not _verify_ket_signature(
        raw_body,
        request.headers.get('X-KET-Timestamp'),
        request.headers.get('X-KET-Signature'),
    ):
        return jsonify({'error': 'invalid signature'}), 401

    # Live in-widget handover is OFF by default (ticket-based escalation only).
    # A valid, signed agent-reply is accepted but intentionally not delivered:
    # no widget message, no flip to human_open. Flip LIVE_HANDOVER_ENABLED only
    # after the joint live-handover acceptance programme signs off.
    if not current_app.config.get('LIVE_HANDOVER_ENABLED', False):
        return jsonify({'status': 'ignored', 'reason': 'live_handover_disabled'}), 202

    data = request.get_json(force=True, silent=True) or {}
    session_id = data.get('session_id', '').strip()
    content = data.get('content', '').strip()
    agent_name = data.get('agent_name', 'Agent')

    if not session_id or not content:
        return jsonify({'error': 'session_id and content required'}), 400

    db = _get_db()
    cur = db.cursor()

    cur.execute(
        "SELECT session_id, status FROM chat_sessions WHERE session_id = %s",
        (session_id,)
    )
    session = cur.fetchone()
    if not session:
        db.close()
        return jsonify({'error': 'session not found'}), 404

    if session['status'] == 'archived':
        db.close()
        return jsonify({'error': 'session is archived'}), 409

    # Idempotency: KET may retry a webhook. If the latest agent message already has
    # identical content, treat this as a duplicate delivery and no-op (return 200).
    cur.execute(
        """SELECT id, content FROM chat_messages
           WHERE session_id = %s AND source = 'human'
           ORDER BY id DESC LIMIT 1""",
        (session_id,)
    )
    _last = cur.fetchone()
    if _last and (_last['content'] or '') == content:
        db.close()
        return jsonify({'status': 'delivered', 'session_id': session_id,
                        'message_id': _last['id'], 'duplicate': True})

    # Insert agent message
    msg_id = _insert_message(db, session_id, 'human', 'assistant', content,
                    metadata={'agent_name': agent_name, 'source': 'ket'})
    _log_event(db, session_id, 'agent_reply', {
        'agent_name': agent_name, 'source': 'ket'
    })

    # Flip to human_open — AI goes silent, human handles from here
    cur.execute(
        "UPDATE chat_sessions SET status = 'human_open', last_activity = NOW() WHERE session_id = %s",
        (session_id,)
    )

    db.close()
    return jsonify({'status': 'delivered', 'session_id': session_id, 'message_id': msg_id})


@bp.route('/active-sessions', methods=['GET'])
def chat_active_sessions():
    # KET pull model retired (migrated to push). Endpoint disabled to stop public session leak.
    return jsonify({"error": "Not found"}), 404
    """List active chat sessions for KET agent dashboard.
    Query params: site (site1|site2), status (comma-separated), limit (int)."""
    site_filter = request.args.get('site', '')
    status_filter = request.args.get('status', 'active,human_pending,human_open')
    limit = min(int(request.args.get('limit', 50)), 200)

    statuses = [s.strip() for s in status_filter.split(',') if s.strip()]
    placeholders = ','.join(['%s'] * len(statuses))

    db = _get_db()
    cur = db.cursor()

    query = f"""SELECT session_id, contact_name, contact_email, status,
                       current_page_url, created_at, last_activity
                FROM chat_sessions
                WHERE status IN ({placeholders})
                ORDER BY last_activity DESC
                LIMIT %s"""
    cur.execute(query, (*statuses, limit))
    sessions = cur.fetchall()

    result = []
    for s in sessions:
        _cpu = (s['current_page_url'] or '')
        site = 'in.optiwar.com' if ('in.optiwar.com' in _cpu or 'optiwar.in' in _cpu) else 'optiwar.com'
        ket_site = 'site2' if site == 'in.optiwar.com' else 'site1'

        if site_filter and ket_site != site_filter:
            continue

        # Get last 3 messages
        cur.execute(
            """SELECT role, content, created_at FROM chat_messages
               WHERE session_id = %s ORDER BY created_at DESC LIMIT 3""",
            (s['session_id'],)
        )
        last_msgs = list(cur.fetchall())
        last_msgs.reverse()

        # Get message count
        cur.execute("SELECT COUNT(*) as cnt FROM chat_messages WHERE session_id = %s", (s['session_id'],))
        msg_count = cur.fetchone()['cnt']

        # Check if handover was requested
        cur.execute(
            """SELECT COUNT(*) as cnt FROM chat_events
               WHERE session_id = %s AND event_type = 'agent_reply'""",
            (s['session_id'],)
        )
        has_handover = cur.fetchone()['cnt'] > 0

        result.append({
            'session_id': s['session_id'],
            'contact_name': s['contact_name'] or 'Visitor',
            'contact_email': s['contact_email'] or '',
            'status': s['status'],
            'site': site,
            'ket_site': ket_site,
            'current_page_url': s['current_page_url'] or '',
            'created_at': s['created_at'].isoformat() if s['created_at'] else None,
            'last_activity': s['last_activity'].isoformat() if s['last_activity'] else None,
            'message_count': msg_count,
            'has_handover_request': has_handover,
            'last_messages': [
                {'role': m['role'], 'content': m['content'][:200],
                 'created_at': m['created_at'].isoformat() if m['created_at'] else None}
                for m in last_msgs
            ]
        })

    db.close()
    return jsonify({'sessions': result, 'total': len(result)})


# ─── Text-to-Speech Endpoint ───
@bp.route('/tts', methods=['POST'])
def text_to_speech():
    """Convert text to audio. Primary: ElevenLabs. Fallback: gTTS."""
    import io
    from flask import send_file

    data = request.get_json(force=True, silent=True) or {}
    text = data.get('text', '').strip()
    provider = data.get('provider', 'elevenlabs')  # 'elevenlabs' or 'gtts'

    if not text:
        return jsonify({'error': 'text required'}), 400

    # Limit text length to prevent abuse
    if len(text) > 2000:
        text = text[:2000]

    # Try ElevenLabs first (unless forced to gtts)
    if provider != 'gtts':
        try:
            import requests as req
            el_key = os.environ.get('ELEVENLABS_API_KEY', '')
            if el_key:
                # Use Rachel voice with Flash v2.5 model (ultra-low latency)
                voice_id = '21m00Tcm4TlvDq8ikWAM'
                resp = req.post(
                    f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}',
                    headers={
                        'xi-api-key': el_key,
                        'Content-Type': 'application/json'
                    },
                    json={
                        'text': text,
                        'model_id': 'eleven_flash_v2_5',
                        'voice_settings': {
                            'stability': 0.5,
                            'similarity_boost': 0.75
                        }
                    },
                    timeout=15
                )
                if resp.status_code == 200:
                    audio_io = io.BytesIO(resp.content)
                    audio_io.seek(0)
                    return send_file(audio_io, mimetype='audio/mpeg', download_name='speech.mp3')
                else:
                    current_app.logger.warning(f'[TTS] ElevenLabs failed ({resp.status_code}): {resp.text[:200]}')
        except Exception as e:
            current_app.logger.warning(f'[TTS] ElevenLabs error: {e}')

    # Fallback: gTTS (Google Text-to-Speech)
    try:
        from gtts import gTTS
        tts = gTTS(text=text, lang='en', slow=False)
        audio_io = io.BytesIO()
        tts.write_to_fp(audio_io)
        audio_io.seek(0)
        return send_file(audio_io, mimetype='audio/mpeg', download_name='speech.mp3')
    except Exception as e:
        current_app.logger.error(f'[TTS] gTTS fallback failed: {e}')
        return jsonify({'error': 'TTS generation failed'}), 500
