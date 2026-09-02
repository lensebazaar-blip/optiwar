import requests as http_requests
import random
import functools
from .mail import send_order_confirmation
from .payments import initiate_payment, PaytmChecksum, verify_payment_status, create_razorpay_order, verify_razorpay_payment, fetch_razorpay_payment, verify_razorpay_webhook, verify_razorpay_payment_link
from .paid_orders import (apply_paid_order, append_status, order_amount_minor,
                          order_currency, order_payment_state)
from .razorpay_events import PAID_EVENTS, payment_entity
from .rx_powers import normalize_rows
from . import ops_refunds
from flaskr.notifications import notify_payment_attempted, notify_payment_success, notify_payment_failed, notify_order_confirmed, notify_order_shipped
import os
import MySQLdb
from flask import (
    Blueprint, flash, g, render_template, request, redirect, url_for, current_app, session, make_response, jsonify, json, send_from_directory, abort
)
from .db import get_db
from .embed_helper import (
    build_media_list, build_media_primary, build_media_one, MEDIA_SCHEMA_VERSION,
    versioned_image_url, versioned_angle_urls, frame_shape, is_merchant_eligible,
)
from .catalogue import (
    catalogue_site_filter, is_product_allowed, is_contact_lens, sellable_here,
    current_site, strip_ineligible_urls, age_group, ensure_gmc_columns,
    live_lenses, lens_matrix_summary, SITE_IN, SITE_COM,
)
from . import acr, lens_feed, lens_order, lens_seo, lens_view
from .cart_persist import save_cart_to_db, clear_cart_in_db
from .cl_range_model import add_prescription_of_cl
from .country_iso import country_to_iso2
import re
import ast
from datetime import datetime, timedelta
from openai import OpenAI
import redis
import json as json_mod
from datetime import datetime
import unicodedata


bp = Blueprint('main', __name__)

# The EU Ops refund API lives on this blueprint because __init__.py is outside
# the deployment set: a blueprint of its own could not be registered without
# editing a file the deploy tool cannot safely replace.
ops_refunds.register(bp)

@bp.route('/eu/')
@bp.route('/eu/<path:rest>')
def eu_redirect(rest=''):
    """D4: Redirect old /eu/ subdirectory URLs to canonical root URLs."""
    target = f'/{rest}' if rest else '/'
    qs = request.query_string.decode()
    if qs:
        target += f'?{qs}'
    return redirect(target, code=301)



def _req_is_india():
    """True if the current request host is an India storefront
    (in.optiwar.com legacy or optiwar.in). Central India-detection helper."""
    try:
        h = request.host.lower()
    except Exception:
        return False
    return 'in.optiwar.com' in h or 'optiwar.in' in h


def _get_site_from():
    """Returns 'in.optiwar.com' (internal India token) or 'optiwar.com'."""
    try:
        host = request.host.lower()
        if 'in.optiwar.com' in host or 'optiwar.in' in host:
            return 'in.optiwar.com'
    except:
        pass
    return 'optiwar.com'

# ===== GEO-CURRENCY DETECTION =====
EUR_RATE = 93.0  # 1 EUR ≈ 93 INR (update periodically)


# Redis connection for caching
_redis_client = None
def get_redis():
    global _redis_client
    if _redis_client is None:
        try:
            _redis_client = redis.Redis(host='127.0.0.1', port=6379, db=0, decode_responses=True)
            _redis_client.ping()
        except Exception:
            _redis_client = None
    return _redis_client


def get_country_from_ip(ip):
    """Get country code from IP using free ip-api.com."""
    try:
        if ip in ('127.0.0.1', '::1', 'localhost'):
            return 'IN'
        resp = http_requests.get(f'http://ip-api.com/json/{ip}?fields=countryCode', timeout=2)
        if resp.status_code == 200:
            return resp.json().get('countryCode', 'XX')
    except:
        pass
    return 'XX'

def inr_to_eur(inr_price):
    """Convert INR to EUR with rounding."""
    if not inr_price:
        return None
    import math
    raw = float(inr_price) / EUR_RATE
    if raw < 10:
        base = math.floor(raw)
        frac = raw - base
        if frac < 0.25:
            return max(base - 0.01, round(raw, 2))
        elif frac < 0.75:
            return base + 0.49
        else:
            return base + 0.99
    elif raw < 50:
        return math.floor(raw) + 0.99
    else:
        base = math.floor(raw / 5) * 5
        return base + 4.99

@bp.before_request
def detect_currency():
    """Set currency to INR."""
    if request.path.startswith('/static/'):
        return
    session['currency'] = 'INR'
    session['region'] = 'in'

@bp.context_processor
def inject_currency():
    """Make currency info available in all templates."""
    from flask import request
    _india = _req_is_india()
    return {
        'current_currency': 'INR' if _india else 'EUR',
        'current_region': 'in' if _india else 'eu',
        'is_eur': not _india,
        'currency_symbol': '₹' if _india else '€',
        'eur_rate': EUR_RATE,
        'google_maps_api_key': GOOGLE_MAPS_API_KEY
    }



def _add_business_days(start_date, n):
    """Add n days to start_date, skipping Sundays (weekday() == 6)."""
    current_date = start_date
    days_added = 0
    while days_added < n:
        current_date += timedelta(days=1)
        if current_date.weekday() != 6:
            days_added += 1
    return current_date


def dispatch_date_obj(start_date=None):
    """Estimated dispatch date: 2 business days from start (skipping Sundays)."""
    if not start_date:
        start_date = datetime.now()
    return _add_business_days(start_date, 2)


def calculate_ship_date(start_date=None):
    return dispatch_date_obj(start_date).strftime('%A, %d %B %Y')

def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

def fetch_dict_rows(cursor):
    """Convert MySQLdb tuple result to list of dicts."""
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]



def extract_search_intent(query, client):
    prompt = f"""
You are a smart optician product search assistant for an e-commerce system.

When users type product-related search queries, extract two things:

1. A list of **keywords** for fuzzy matching in product name, category, code, color, perception.
2. A dictionary of **exact filters** that match internal fields in our database schema.

Special behaviours
- If user types something like "AJ 7", "AJ77", "BC87", assume it's a **product_code** or part of it.
- If it looks like a brand/model code, return it under keywords, or use product_code filter if exact.

Our schema includes:
- `product_type`: "full-rim", "semi-rimless", "rimless"
- `product_category`: "Spectacles Frame", "Sunglasses", etc.
- `product_category_rimless`: a numeric flag (1 if rimless frame)
- `product_category_kids`: a numeric flag (1 if kids frame)
- `product_category_adults`: a numeric flag (1 if adults frame)
- `product_category_rectangle`: a numeric flag (1 if rectangular frame)
- `product_category_oval`: a numeric flag (1 if oval frame)
- `product_category_round`: a numeric flag (1 if round frame)
- `product_category_square`: a numeric flag (1 if square frame)
- `product_category_wayfarer`: a numeric flag (1 if wayfarer frame)
- `product_category_horn`: a numeric flag (1 if horn frame)
- `product_category_browline`: a numeric flag (1 if browline frame)
- `product_category_aviator`: a numeric flag (1 if aviator frame)
- `product_category_cateye`: a numeric flag (1 if cateye frame)
- `product_category_semirimless`: a numeric flag (1 if semirimless frame or has less rims)
- `product_category_supra`: a numeric flag (1 if supra/half-frame)
- `product_category_clubmaster`: a numeric flag (1 if clubmaster style)
- `product_category_quatra`: a numeric flag (1 if quatra style)
- `product_category_panto`: a numeric flag (1 if panto style)
- `product_size`: values like "small", "medium", "large"
- `product_color`, `product_code`, etc.

Some examples:

- Query: "rimless frame"
  → keywords: ["frame"]
  → filters: {{ "product_category_rimless": 1 }}


- Query: "frameless"
  → keywords: ["frame"]
  → filters: {{ "product_category_rimless": 1 }}

- Query: "3 piece frame"
  → keywords: ["frame"]
  → filters: {{ "product_category_rimless": 1, "product_category": "Spectacles Frame" }}

- Query: "green half frame spectacles"
  → keywords: ["green", "spectacles"]
  → filters: {{ "product_type": "semi-rimless", "product_category": "Spectacles Frame" }}

- Query: "gold full frame specs"
  → keywords: ["gold", "specs"]
  → filters: {{ "product_type": "full-rim", "product_category": "Spectacles Frame" }}

- "AJ 7" →  keywords: ["AJ77"], filters: {{ "product_code": }}

Always output a valid Python dictionary:
{{
  "keywords": [...],
  "filters": {{"field_name": "value", ...}}
}}

Query: "{query}"
Result:
"""
    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}]
        )
        content = response.choices[0].message.content.strip()
        print("🧠 GPT Search Intent Response: %s" % content)
        return ast.literal_eval(content)
    except Exception as e:
        print("❌ GPT intent extraction failed: %s" % e)
        return {"keywords": query.lower().split(), "filters": {}}




def build_product_meta(product):
    # Ensure minimum viable data is present
    if not product.get('product_name') or not product.get('product_size'):
        return {}

    title = f"{product['product_name']} - Size {product['product_size']}"
    description_parts = []

    # Add valid fields conditionally
    if product.get('product_category') and product['product_category'] != "0":
        description_parts.append(f"Category: {product['product_category']}")

    if product.get('product_category_supra') == 1:
        description_parts.append("This is a Supra Frame")

    if product.get('product_color') and product['product_color'] != "0":
        description_parts.append(f"Color Options: {product['product_color']}")

    if product.get('product_perception_value'):
        description_parts.append(f"Perception: {product['product_perception_value']}")

    if product.get('product_country_of_manufacture'):
        description_parts.append(f"Made in {product['product_country_of_manufacture']}")

    # Join all pieces into a concise meta description
    description = ". ".join(description_parts)[:160]

    # Compose keywords list
    keywords = [
        product['product_name'],
        product.get('product_category', ''),
        product.get('product_color', '')
    ]
    keywords = ", ".join(filter(None, keywords))

    return {
        'title': title,
        'description': description,
        'keywords': keywords
    }



client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY', ''))



@bp.route('/search')
def search_products():
    _host = request.host
    _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    _uid = session.get('user_id', 'anon')
    db = get_db()
    cursor = db.cursor()
    query = request.args.get('query', '').strip()
    current_app.logger.info(f"[{_host}] ACTIVITY:SEARCH IP:{_ip} user:{_uid} query:{query}")
    products = []

    # Get user info for chat template
    _user_info = None
    if 'user_id' in session:
        try:
            _cu2 = db.cursor()
            _cu2.execute('SELECT customer_name, customer_email, customer_phone FROM customers WHERE customer_id = %s', (session['user_id'],))
            _ur2 = _cu2.fetchone()
            if _ur2:
                _user_info = {
                    "name": _ur2.get("customer_name", "") if isinstance(_ur2, dict) else _ur2[0],
                    "email": _ur2.get("customer_email", "") if isinstance(_ur2, dict) else _ur2[1],
                    "phone": _ur2.get("customer_phone", "") if isinstance(_ur2, dict) else _ur2[2]
                }
        except:
            pass

    if not query:
        print("⚠️ Empty query received.")
        return render_template('search.html', products=[], query=query, user_info=_user_info, chat_timeout_minutes=10)

    print("🔍 Search query: %s", query)

    # === Extract intent via GPT ===
    client = OpenAI(api_key=os.environ.get('OPENAI_API_KEY', ''))  # ideally from env/config
    intent = extract_search_intent(query, client)
    tokens = intent.get("keywords", [])
    filters = intent.get("filters", {})

    print("🧠 Tokens: %s", tokens)
    print("🔒 Filters: %s", filters)

    # === Build LIKE clause from tokens ===
    like_clauses = []
    like_params = []
    if "product_code" in filters:
       raw_code = filters["product_code"]
       fuzzy_code = raw_code.replace(" ", "").upper()
       filters.pop("product_code")  # Remove exact match filter
       tokens.append(fuzzy_code)    # Treat as keyword for LIKE
       print("🔁 Fuzzy fallback: converted product_code '%s' to keyword token '%s'", raw_code, fuzzy_code)



    for token in tokens:
        like_clauses.append("""(LOWER(product_name) LIKE %s OR
                                 LOWER(product_category) LIKE %s OR
                                 LOWER(product_perception_value) LIKE %s OR
                                 LOWER(product_code) LIKE %s OR
                                 LOWER(product_color) LIKE %s)""")
        like_params.extend([f"%{token}%"] * 5)

    # === Build exact match filters ===
    filter_clauses = []
    filter_params = []
    for key, val in filters.items():
        filter_clauses.append(f"{key} = %s")
        filter_params.append(val)

    # Combine WHERE clause
    where_parts = []
    if like_clauses:
        where_parts.append(f"({' OR '.join(like_clauses)})")
    if filter_clauses:
        where_parts.append(" AND ".join(filter_clauses))
    where_clause = " AND ".join(where_parts)

    # === SQL query ===
    sql = f"""
        SELECT * FROM products
        WHERE {where_clause}
        AND product_image IS NOT NULL AND product_image != '' AND product_image != 'NULL'
        AND product_quantity > 0
        {catalogue_site_filter()}
        ORDER BY product_name
        LIMIT 20
    """

    try:
        # Log search
        user_ip = request.remote_addr
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            "INSERT INTO search_user_log (search_query, ip_address, search_time) VALUES (%s, %s, %s)",
            (query, user_ip, timestamp)
        )
        db.commit()

        # Execute search
        print("📄 SQL: %s", sql)
        print("📦 Params: %s", like_params + filter_params)
        cursor.execute(sql, like_params + filter_params)
        products = cursor.fetchall()
        print("✅ Found %d products for query: %s", len(products), query)

    except Exception as e:
        print("🔥 Search execution failed: %s", e)

    # Generate AI summary for the results
    ai_summary = ""
    if products:
        count = len(products)
        categories = list(set(p.get('product_category', '') for p in products if p.get('product_category')))
        cat_str = ", ".join(categories[:3]) if categories else "products"
        if count == 1:
            p = products[0]
            ai_summary = f"I found exactly 1 match — {p.get('product_name', 'a product')} in {p.get('product_category', 'our catalog')}. Here it is:"
        elif count <= 5:
            ai_summary = f"I found {count} {cat_str} matching \"{query}\". Here are your results:"
        else:
            ai_summary = f"Great news! I found {count} {cat_str} matching \"{query}\". Browse through the results below:"
        # Add price range info
        prices = [p.get('product_special_price', 0) for p in products if p.get('product_special_price')]
        if prices:
            min_p, max_p = min(prices), max(prices)
            if min_p != max_p:
                ai_summary += f" Prices range from Rs {min_p} to Rs {max_p}."
            else:
                ai_summary += f" Priced at Rs {min_p}."
    elif query:
        ai_summary = f"I searched our catalog for \"{query}\" but couldn't find matching products. Try different keywords, or call our specialist at 9355380318."

    return render_template('search.html', products=products, query=query, ai_summary=ai_summary, user_info=_user_info, chat_timeout_minutes=10)




@bp.route('/api/search')
def api_search():
    """AJAX search endpoint — returns JSON with products + AI summary."""
    db = get_db()
    cursor = db.cursor()
    query = request.args.get('query', '').strip()
    products = []
    ai_summary = ""

    if not query:
        return jsonify({"products": [], "ai_summary": "Please enter a search query.", "query": ""})

    print("🔍 API Search query: %s" % query)

    # === Extract intent via GPT with timeout ===
    try:
        client_api = OpenAI(
            api_key=os.environ.get('OPENAI_API_KEY', ''),
            timeout=8.0
        )
        intent = extract_search_intent(query, client_api)
    except Exception as e:
        print("⚠️ GPT intent extraction failed/timed out: %s" % e)
        intent = {"keywords": query.lower().split(), "filters": {}}

    tokens = intent.get("keywords", [])
    filters = intent.get("filters", {})

    # === Build LIKE clause from tokens ===
    like_clauses = []
    like_params = []
    if "product_code" in filters:
        raw_code = filters["product_code"]
        fuzzy_code = raw_code.replace(" ", "").upper() if raw_code else ""
        filters.pop("product_code")
        if fuzzy_code:
            tokens.append(fuzzy_code)

    for token in tokens:
        if not token:
            continue
        like_clauses.append("""(LOWER(product_name) LIKE %s OR
                                 LOWER(product_category) LIKE %s OR
                                 LOWER(product_perception_value) LIKE %s OR
                                 LOWER(product_code) LIKE %s OR
                                 LOWER(product_color) LIKE %s)""")
        like_params.extend([f"%{token}%"] * 5)

    filter_clauses = []
    filter_params = []
    for key, val in filters.items():
        filter_clauses.append(f"{key} = %s")
        filter_params.append(val)

    where_parts = []
    if like_clauses:
        where_parts.append(f"({' OR '.join(like_clauses)})")
    if filter_clauses:
        where_parts.append(" AND ".join(filter_clauses))

    if not where_parts:
        return jsonify({"products": [], "ai_summary": f"I couldn\'t understand the search \"{query}\". Try searching for product names, colors, or codes like \'AJ77\' or \'gold rimless\'.", "query": query})

    where_clause = " AND ".join(where_parts)

    sql = f"""
        SELECT * FROM products
        WHERE {where_clause}
        AND product_image IS NOT NULL AND product_image != '' AND product_image != 'NULL'
        AND product_quantity > 0
        {catalogue_site_filter()}
        ORDER BY product_quantity DESC, product_name
        LIMIT 20
    """

    try:
        user_ip = request.remote_addr
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            "INSERT INTO search_user_log (search_query, ip_address, search_time) VALUES (%s, %s, %s)",
            (query, user_ip, timestamp)
        )
        db.commit()

        cursor.execute(sql, like_params + filter_params)
        products = cursor.fetchall()
        print("✅ API Search found %d products for: %s" % (len(products), query))
    except Exception as e:
        print("🔥 API Search failed: %s" % e)
        products = []

    # Build AI summary
    if products:
        count = len(products)
        categories = list(set(p.get('product_category', '') for p in products if p.get('product_category')))
        cat_str = " and ".join(categories[:3]) if categories else "products"
        if count == 1:
            p = products[0]
            ai_summary = f"Found exactly 1 match — <strong>{p.get('product_name', 'a product')}</strong> in {p.get('product_category', 'our catalog')}."
        elif count <= 5:
            ai_summary = f"Found <strong>{count}</strong> {cat_str} matching your search."
        else:
            ai_summary = f"Great news! Found <strong>{count}</strong> {cat_str} matching \"{query}\"."
        prices = [p.get('product_special_price', 0) for p in products if p.get('product_special_price')]
        if prices:
            mn, mx = min(prices), max(prices)
            if mn != mx:
                ai_summary += f" Prices range from <strong>Rs {mn}</strong> to <strong>Rs {mx}</strong>."
            else:
                ai_summary += f" Priced at <strong>Rs {mn}</strong>."
        in_stock = sum(1 for p in products if p.get('product_quantity', 0) > 0)
        if in_stock < count:
            ai_summary += f" {in_stock} of {count} are currently in stock."
    else:
        ai_summary = f"I searched our catalog for \"{query}\" but couldn\'t find matching products. Try different keywords, or call our specialist at <strong>9355380318</strong>."

    # Serialize products for JSON (convert non-serializable types)
    serialized = []
    for p in products:
        sp = {}
        for k, v in p.items():
            if isinstance(v, (int, float, str, bool, type(None))):
                sp[k] = v
            else:
                sp[k] = str(v)
        # Search cards show a single image (no gallery) -> primary media only.
        sp['media'] = build_media_list(p.get('product_image') or '', limit=1)
        serialized.append(sp)

    return jsonify({"products": serialized, "ai_summary": ai_summary, "query": query,
                    "media_schema": MEDIA_SCHEMA_VERSION})


@bp.route("/product_images/<int:product_id>")
def product_images(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT product_image FROM products WHERE product_id = %s"
                   + catalogue_site_filter(), (product_id,))
    row = cursor.fetchone()
    if not row or not row.get('product_image'):
        return jsonify([])

    product_images = row['product_image'] if isinstance(row, dict) else row[0]
    image_list = [img.strip() for img in product_images.split(',')]
    return jsonify(image_list)




@bp.route('/autocomplete')
def autocomplete():
    term = request.args.get('term', '')
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT DISTINCT product_name FROM products WHERE product_name LIKE %s "
                   "AND product_quantity > 0" + catalogue_site_filter() + " LIMIT 10",
                   (f"%{term}%",))
    suggestions = [row for row in cursor.fetchall()]
    cursor.close()
    #conn.close()
    return jsonify(suggestions)







@bp.route('/', methods=['GET', 'POST'])
def index():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM products where product_image!='NULL' AND product_quantity!=0"
                   + catalogue_site_filter() + " order by RAND()")
    products = cursor.fetchall()
    # Homepage grid (scripts.js loadProducts) is inlined for ALL products.
    # Attach PRIMARY media only (limit=1), and dump a TRIMMED projection to JS
    # (only the fields loadProducts actually reads) so the inline payload stays
    # small despite adding media. Server-side Jinja (JSON-LD, hero) still uses
    # the full `products` rows.
    _JS_FIELDS = (
        'product_id', 'product_code', 'product_name', 'product_category',
        'product_color', 'product_image', 'product_size', 'product_slug',
        'product_price', 'product_special_price', 'product_price_eur',
        'product_special_price_eur', 'product_perception_value',
        'form_factor', 'fitting_range', 'channels', 'warranty', 'media',
    )
    # Homepage cards are a multi-angle rotating gallery (desktop) / swipe strip
    # (mobile), so they need the FULL media array, not primary-only.
    for _p in products:
        _p['media'] = build_media_list(_p.get('product_image') or '')
    products_js = [{k: p.get(k) for k in _JS_FIELDS} for p in products]
    #print(products)
    meta = build_product_meta(products[0]) if products else {}
    print("Generated SEO Meta Tags")
    for k, v in meta.items():
        print(f"{k}:{v}")


    # Load lens pricing for Best Selling Lenses section
    import json as _json_idx
    _lens_prices = {}
    try:
        with open("/var/www/flask-optiwar-ow-release-090525/lens_pricing.json", "r") as _fp:
            _lp = _json_idx.load(_fp)
        for _cat in _lp.get("default", {}).values():
            for _code, _item in _cat.items():
                _lens_prices[_code] = {"price": _item["price"], "price_eur": _item.get("price_eur", round((_item["price"] + 3000) / 100, 2))}
    except Exception:
        pass

    # Fetch store-level aggregate rating for OpticalStore JSON-LD
    _store_cursor = db.cursor()
    _store_cursor.execute("SELECT COUNT(*) as cnt, COALESCE(AVG(rating),0) as avg_r FROM product_reviews WHERE is_approved=1")
    _sr = _store_cursor.fetchone()
    _store_review_count = _sr['cnt'] if _sr else 0
    _store_avg_rating = round(_sr['avg_r'], 1) if _sr and _sr['avg_r'] else 0
    _store_cursor.close()

    response = make_response(render_template('index.html', products=products, products_js=products_js,
                                             meta=meta, lens_prices=_lens_prices,
                                             store_review_count=_store_review_count, store_avg_rating=_store_avg_rating))
    return response

@bp.route('/add_to_cart', methods=['POST'])
def add_to_cart():
    product_id = request.form['product_id']
    _cur = get_db().cursor()
    try:
        if not sellable_here(_cur, product_id):
            return "Product not found", 404
    finally:
        _cur.close()
    product_name = request.form['product_name']
    product_special_price = safe_float(request.form['product_special_price'])
    product_code = request.form['product_code']
    product_price = safe_float(request.form['product_price'])
    product_category = request.form.get('product_category', 'category_not_defined')
    order_quantity = int(request.form.get('order_quantity', 1))
    ATC_total = 0
    recommendations = 'Complimentary Plano Anti-Glare Lenses'
    lens_mrp = 0
    #print(request.form)

    if 'cart' not in session:
        session['cart'] = []
    cart = session['cart']


    item_exists = False
    for item in cart:
        if item['product_id'] == product_id and item.get('right_eye') == "No RX selected" and item.get('left_eye') == "No RX selected":
            item['order_quantity'] +=1
            item['ATC_total'] = safe_float(item['product_special_price']) * item['order_quantity']
            item['full_product_price'] = product_price + lens_mrp
            item['total_savings'] = (product_price + lens_mrp) - (ATC_total) 
            item['product_code'] = item.get('product_code',0)
            item['product_special_price'] = item.get('product_special_price', 0)
            item['product_price'] = item.get('product_price', 0)
            #item['server_total_price'] = item.get('server_total_price', 0) + item['ATC_total']
            item['server_total_price'] = item.get('server_total_price', 0)
            item['product_category'] = item.get('product_category', 'category_not_defined')
            item_exists = True
            break


    if not item_exists:
        cart.append({'product_id': product_id, 'product_name': product_name, 
                    'product_special_price': product_special_price, 'product_price': product_price, 
                    'order_quantity': 1, 'ATC_total' : product_special_price * order_quantity, 
                    'full_product_price': product_price + lens_mrp,
                    'total_savings' : product_price + (product_special_price * order_quantity),
                    'product_code': product_code, 
                    'rx_id': None, 
                    'right_eye': "No RX selected", 'left_eye': "No RX selected", 'lens_price': 0, 'cyl_price_increase': 0, 
                     'add_price_increase': 0, 'recommendations': recommendations, 'product_category': product_category})

    session['cart'] = cart
    # Sync cart to DB for cross-device persistence
    if session.get('user_id'):
        save_cart_to_db()
    _host = request.host
    _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    _uid = session.get('user_id', 'anon')
    current_app.logger.info(f"[{_host}] ACTIVITY:ADD_TO_CART IP:{_ip} user:{_uid} product:{product_id} name:{product_name} code:{product_code} price:{product_special_price} qty:{order_quantity}")
    return redirect(url_for('main.checkout'))


@bp.route('/contact_lenses', methods=['GET', 'POST'])
def contact_lenses():
    # The listing this replaces selected on product_category and showed whatever
    # was in the table; the canonical one shows released lenses only. Kept as a
    # redirect so one URL is indexable rather than two showing different sets.
    if current_site() == SITE_IN:
        return "Not found", 404
    return redirect(lens_seo.ROOT_PATH, code=301)


def _lens_landing(facet_slug=None, brand_slug=None):
    """Render a contact-lens shelf, or 404 when this storefront has no such page.

    404 rather than an empty listing for a facet or brand nothing is released in:
    the release flag decides what exists, so a shelf appears the day a lens is
    released and not before.
    """
    if current_site() == SITE_IN:
        return "Not found", 404
    db = get_db()
    cursor = db.cursor()
    # Lens availability is IN_STOCK/ON_ORDER on the lens profile and is never
    # frame quantity: lenses are continuously replenished and do not deplete, so
    # filtering on product_quantity here would hide the whole catalogue.
    rows = live_lenses(cursor, SITE_COM)
    for row in rows:
        row['images'] = lens_feed.lens_images(cursor, row['product_id'])
    view = lens_seo.landing_page(rows, 'https://optiwar.com',
                                 facet_slug=facet_slug, brand_slug=brand_slug)
    if not view:
        return "Not found", 404
    return make_response(render_template(
        'lens_landing.html', page=view, brands=view['brands'],
        shelves=view['shelves'], jsonld=view['jsonld'],
        root_path=lens_seo.ROOT_PATH))


@bp.route('/contact-lenses')
def lens_index():
    return _lens_landing()


@bp.route('/contact-lenses/brand/<brand_slug>')
def lens_brand(brand_slug):
    return _lens_landing(brand_slug=brand_slug)


def _released_lens(cursor, product_id):
    """One released lens by id, or ``None``: the same gate every surface reads."""
    return next((r for r in live_lenses(cursor, SITE_COM)
                 if str(r['product_id']) == str(product_id or '')), None)


def _lens_choices(cursor, lens):
    """What this lens states as orderable, in whichever shape it states it."""
    if (lens.get('param_mode') or '').strip().upper() == 'RULES':
        return lens_order.selectable(
            lens_order.param_rules(cursor, lens['product_id']),
            lens.get('lens_type'))
    return lens_order.selectable(lens_order.variants(cursor,
                                                     lens['product_id']))


def _lens_selection(lens, shape, errors=(), submitted=None):
    return make_response(render_template(
        'lens_select.html', lens=lens,
        options=shape.options(),
        minimums=lens_order.minimums(lens, SITE_COM),
        box_price=lens_order.box_price(lens),
        errors=list(errors), submitted=submitted or {},
        eyes=lens_order.EYES,
        max_boxes=lens_order.MAX_BOXES_PER_EYE))


@bp.route('/contact-lenses/select', methods=['GET', 'POST'])
def lens_select():
    """Choose a prescription per eye, from the combinations that exist."""
    if current_site() == SITE_IN:
        return "Not found", 404
    cursor = get_db().cursor()
    lens = _released_lens(cursor, request.values.get('product_id'))
    if not lens:
        return "Product not found", 404
    return _lens_selection(lens, _lens_choices(cursor, lens))


@bp.route('/contact-lenses/add', methods=['POST'])
def lens_add_to_cart():
    """Add a validated per-eye order: boxes × box price, priced from the row.

    The posted price is ignored. What is charged is the catalogue's EUR box
    price times the boxes, and a combination the matrix does not hold is
    refused rather than ordered from a manufacturer who does not make it.
    """
    if current_site() == SITE_IN:
        return "Not found", 404
    db = get_db()
    cursor = db.cursor()
    lens = _released_lens(cursor, request.form.get('product_id'))
    if not lens:
        return "Product not found", 404
    shape = _lens_choices(cursor, lens)
    selections = [lens_order.read_eye(request.form, eye)
                  for eye in lens_order.EYES]
    lines, problems = lens_order.validate_detailed(shape, lens, selections,
                                                   site=SITE_COM)
    if problems:
        acr.log_event(db, acr.EV_LENS_ORDER_REFUSED,
                      failure_code=problems[0][0], success=False,
                      payload={'product_id': str(lens['product_id']),
                               'reasons': sorted({c for c, _ in problems})})
        return _lens_selection(lens, shape, [m for _, m in problems],
                               request.form)
    item = lens_order.cart_item(lens, lines)
    cart = [i for i in session.get('cart', [])
            if str(i.get('product_id')) != item['product_id']]
    cart.append(item)
    session['cart'] = cart
    session.modified = True
    if session.get('user_id'):
        save_cart_to_db()
    acr.log_event(db, acr.EV_LENS_ORDER_VALIDATED, success=True,
                  payload={'product_id': item['product_id'],
                           'boxes': item['order_quantity'],
                           'eyes': [ln['eye'] for ln in lines],
                           'availability': item['availability']})
    current_app.logger.info(
        '[%s] ACTIVITY:ADD_TO_CART_LENS user:%s product:%s boxes:%s total:%s',
        request.host, session.get('user_id', 'anon'), item['product_id'],
        item['order_quantity'], item['ATC_WCL'])
    return redirect(url_for('main.checkout'))


@bp.route('/contact-lenses/<facet_slug>')
def lens_facet(facet_slug):
    return _lens_landing(facet_slug=facet_slug)


@bp.route('/contact_lenses/<categories>')
def list_category():
      if category in categories:
          return render_template(f"{category}/category.html", category=categories[categories])
      else:
          return "Category not found", 404

@bp.route('/contact_lenses/<categories>/<products>')
def show_product(category, product):
    if category in categories and product in categories[category]['products']:
       return render_template(f"{category}/{product}.html", product=product, category=categories[category])
    else:
       return "Product not found", 404


@bp.route('/add_to_cart_wcl', methods=['GET', 'POST'])
def add_to_cart_wcl():
    if request.method != 'POST':
        return redirect(url_for('main.checkout'))

    try:
        db = get_db()
        cursor = db.cursor()

        # Extract and sanitize form inputs
        product_id = request.form.get('product_id', '').strip()
        # Storefront eligibility, before anything is priced or stored: the form
        # carries the price and the parameters, so this is the only place the
        # .in invariant can be enforced for a lens someone posts directly.
        if not sellable_here(cursor, product_id):
            return "Product not found", 404
        product_name = request.form.get('product_name', '').strip()
        product_price = safe_float(request.form.get('product_price', 0))
        product_code = request.form['product_code']
        product_special_price = safe_float(request.form.get('product_special_price', 0))
        product_category = request.form.get('product_category', '').strip()

        right_pwr = request.form.get('right_pwr', '').strip()
        right_cyl = request.form.get('right_cyl', '').strip()
        right_qty = int(request.form.get('right_qty', 0))

        left_pwr = request.form.get('left_pwr', '').strip()
        left_cyl = request.form.get('left_cyl', '').strip()
        left_qty = int(request.form.get('left_qty', 0))

        right_lens_color = request.form.get('right_lens_color', '').strip()
        left_lens_color = request.form.get('left_lens_color', '').strip()

        order_quantity = right_qty + left_qty

        right_eye = f"{right_pwr}/{right_cyl}/{right_qty}/{right_lens_color}"
        left_eye = f"{left_pwr}/{left_cyl}/{left_qty}/{left_lens_color}"
        recommendations = product_name
        recommendation_price = product_special_price

        current_app.logger.info(
            f'🛒 Updating Cart >> Product: {product_name}, Qty: {order_quantity}, Right PWR: {right_pwr}, Left PWR: {left_pwr}'
        )


        # Insert RX details
        cursor.execute(
            'INSERT INTO rx_collector (recommendations, recommendation_price, right_eye, left_eye, product_id) '
            'VALUES (%s, %s, %s, %s, %s)',
            (recommendations, recommendation_price, right_eye, left_eye, product_id)
        )
        rx_id = cursor.lastrowid
        db.commit()


        # Initialize or update cart in session
        cart = session.get('cart', [])
        item_found = False

        for item in cart:
            if (item['product_id'] == product_id and
                item['right_lens_color'] == right_lens_color and
                item['left_lens_color'] == left_lens_color):
                item.update({
                    'product_special_price': product_special_price,
                    'product_price': product_price,
                    'product_code': product_code,
                    'product_name': product_name,
                    'product_category': product_category,
                    'order_quantity': order_quantity,
                    'full_product_price': product_price,
                    'ATC_WCL': right_qty * product_special_price + left_qty * product_special_price,
                    'right_pwr': right_pwr,
                    'right_lens_color': right_lens_color,
                    'right_cyl': right_cyl,
                    'right_qty': right_qty,
                    'left_pwr': left_pwr,
                    'left_cyl': left_cyl,
                    'left_qty': left_qty,
                    'left_lens_color': left_lens_color,
                    'rx_id': rx_id
                })
                item_found = True
                break

        if not item_found:
            cart.append({
                'product_id': product_id,
                'product_special_price': product_special_price,
                'product_price': product_price,
                'product_code': product_code,
                'product_name': product_name,
                'product_category': product_category,
                'full_product_price': product_price,
                'order_quantity': order_quantity,
                'ATC_WCL': right_qty * product_special_price + left_qty * product_special_price,
                'right_pwr': right_pwr,
                'right_lens_color': right_lens_color,
                'right_cyl': right_cyl,
                'right_qty': right_qty,
                'left_pwr': left_pwr,
                'left_cyl': left_cyl,
                'left_qty': left_qty,
                'left_lens_color': left_lens_color,
                'rx_id': rx_id
            })


        # Add rx_id to session
        rx_ids = session.get('rx_ids', [])
        rx_ids.append(rx_id)
        session['rx_ids'] = rx_ids
        session['cart'] = cart
        session.modified = True
        # Sync cart to DB for cross-device persistence
        if session.get("user_id"):
            save_cart_to_db()

        current_app.logger.info(f'🧾 New RX record created with rx_id={rx_id} for product {product_id}')
        current_app.logger.debug(f'Cart updated: {cart}')

        return redirect(url_for('main.checkout'))

    except Exception as e:
        db.rollback()
        current_app.logger.error(f'❌ Error in add_to_cart_wcl: {str(e)}', exc_info=True)
        flash('An error occurred while processing your request. Please try again.', 'error')
        return redirect(url_for('main.checkout'))



''' LOCKED
@bp.route('/add_to_cart_wcl', methods=['GET', 'POST'])
def add_to_cart_wcl():
    product_id = request.form.get('product_id')
    product_name = request.form.get('product_name')
    product_price = request.form.get('product_price')
    product_special_price = float(request.form.get('product_special_price', 0))
    product_category = request.form.get('product_category', 0)
    #right_pwr_raw = request.form.get('right_pwr', '').strip()
    right_pwr = request.form.get('right_pwr', 0)
    right_cyl = request.form.get('right_cyl', 0)
    right_qty = int(request.form.get('right_qty', 0))
    left_pwr = request.form.get('left_pwr', 0)
    left_cyl = request.form.get('left_cyl', 0)
    left_qty = int(request.form.get('left_qty', 0))
    right_lens_color = request.form.get('right_lens_color', 0)
    left_lens_color = request.form.get('left_lens_color', 0)
    order_quantity = right_qty + left_qty
    print(f'Product Name: {product_name} (Type: {type(product_name)}) Product Special Price: (Type: {type(product_special_price)}) Right PWR: {right_pwr}  (Type: {type(right_pwr)}) Right QTY: (Type: {type(right_qty)}) Left PWR: {left_pwr} (Type: {type(left_pwr)})  Left QTY: (Type: {type(left_qty)}) Right Lens Color: {right_lens_color} (Type: {type(right_lens_color)}) Left Lens Color: {left_lens_color} (Type: {type(left_lens_color)}) Total CL Quantity: {order_quantity} (Type: {type(order_quantity)})')

    db = get_db()
    cursor = db.cursor()

    cart = session.get('cart', [])
    item_exist = False
    for item in cart:
        if item['product_id'] == product_id:
           item['product_special_price'] = product_special_price
           item['product_name'] = product_name
           item['product_price'] = product_price
           item['product_category'] = product_category
           item['order_quantity'] = right_qty + left_qty
           item['full_product_price'] = product_price 
           item['ATC_WCL'] =  float(item['right_qty']) * float(item['product_special_price']) + float(item['left_qty']) * float(item['product_special_price'])
           item['right_pwr'] = right_pwr
           item['right_lens_color'] = right_lens_color
           item['right_cyl'] = right_cyl
           item['right_qty'] = right_qty
           item['left_pwr'] = left_pwr
           item['left_cyl'] = left_cyl
           item['left_qty'] = left_qty
           item['left_lens_color'] = left_lens_color
           item_exist = True
           break

    if not item_exist:
       cart.append({ 
       'product_id': product_id,
        'product_special_price': product_special_price,
        'product_price' : product_price,
        'product_name' : product_name,
        'full_product_price': product_price,
        'product_category' : product_category,
        'ATC_WCL':  right_qty*product_special_price+left_qty*product_special_price,
        'order_quantity' : order_quantity,
        'right_pwr': right_pwr,
        'right_lens_color': right_lens_color,
        'right_cyl': right_cyl,
        'right_qty': right_qty,
        'left_pwr': left_pwr,
        'left_cyl': left_cyl,
        'left_qty': left_qty,
        'left_lens_color': left_lens_color
        })
    session['cart'] = cart
    

    print(cart)
    return redirect(url_for('main.checkout'))
'''


CATEGORY_QUERIES = {
    'Contact Lenses': 'SELECT * FROM products WHERE product_category="Contact Lenses" AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'Hearing Aids': 'SELECT * FROM products WHERE product_category="Hearing Aids" AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'supra': 'SELECT * FROM products WHERE product_category_supra = 1 AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'rectangle': 'SELECT * FROM products WHERE product_category_rectangle = 1 AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'round': 'SELECT * FROM products WHERE product_category_round = 1 AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'rounded': 'SELECT * FROM products WHERE product_category_round = 1 AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'clubmaster': 'SELECT * FROM products WHERE product_category_clubmaster = 1 AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'rimless': 'SELECT * FROM products WHERE product_category_rimless = 1 AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'square': 'SELECT * FROM products WHERE product_category_square = 1 AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'Spectacles Frame': 'SELECT * FROM products WHERE product_category = "Spectacles Frame" AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
    'panto-frames': 'SELECT * FROM products WHERE product_category_panto = 1 AND product_image IS NOT NULL AND product_image != "" AND product_image != "NULL" AND product_quantity > 0',
}



@bp.route("/eyeglasses/all-spectacle-frames.html")
def all_spectacle_frames():
    seed = random.randint(1, 999999)
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT COUNT(*) as cnt FROM products WHERE product_image IS NOT NULL "
        "AND product_image != '' AND product_image != 'NULL' "
        "AND product_quantity > 0 "
        "AND product_category NOT IN ('Contact Lenses', 'Hearing Aids')"
    )
    total_count = cursor.fetchone()['cnt']
    cursor.execute(
        "SELECT * FROM products WHERE product_image IS NOT NULL "
        "AND product_image != '' AND product_image != 'NULL' "
        "AND product_quantity > 0 "
        "AND product_category NOT IN ('Contact Lenses', 'Hearing Aids') "
        "ORDER BY RAND(%s) "
        "LIMIT 20",
        (seed,)
    )
    products = cursor.fetchall()
    cursor.close()
    # Fetch matching frame IDs for logged-in users (cached in Redis for 30min)
    matching_ids = []
    face_meas = {}
    print(f"[ALL-FRAMES] Session keys: {list(session.keys())}, user_id={session.get('user_id')}, user_name={session.get('user_name')}")
    if 'user_id' in session:
        try:
            user_id = session['user_id']
            rcache = get_redis()
            cache_key = f"face_match:{user_id}"
            meas_key = f"face_meas:{user_id}"

            # Try cache first
            cached_ids = rcache.get(cache_key) if rcache else None
            cached_meas = rcache.get(meas_key) if rcache else None

            if cached_ids and cached_meas:
                matching_ids = json_mod.loads(cached_ids)
                face_meas = json_mod.loads(cached_meas)
                print(f"[ALL-FRAMES] Cache HIT for user {user_id}: {len(matching_ids)} matches")
            else:
                # Cache miss - compute and store
                cursor2 = db.cursor()
                cursor2.execute("""
                    SELECT pd_far, face_width, recommended_length
                    FROM face_measurements WHERE customer_id = %s
                    ORDER BY measured_at DESC LIMIT 1
                """, (user_id,))
                meas = cursor2.fetchone()
                if meas:
                    pd_far = float(meas['pd_far']) if meas['pd_far'] else 63.0
                    face_w = float(meas['face_width']) if meas['face_width'] else 132.0
                    rec_l = int(meas['recommended_length'] or 140)
                    face_meas = {"pd": pd_far, "face_width": face_w, "temple_length": rec_l}
                    cursor2.execute("""
                        SELECT product_id, product_size
                        FROM products
                        WHERE product_category = 'Spectacles Frame'
                        AND product_image IS NOT NULL AND product_image != ''
                        AND product_quantity > 0
                        AND product_size IS NOT NULL AND product_size != ''
                        AND product_size REGEXP '^[0-9]+-[0-9]+-[0-9]+$'
                    """)
                    for row in cursor2.fetchall():
                        parts = str(row['product_size']).split('-')
                        if len(parts) < 3:
                            continue
                        try:
                            d = int(parts[0])
                            b = int(parts[1])
                            l = int(parts[2])
                        except ValueError:
                            continue
                        frame_total = (d * 2) + b + 10
                        if abs(frame_total - face_w) > 8:
                            continue
                        frame_pcd = d + b
                        if abs(frame_pcd - pd_far) / 2.0 > 6:
                            continue
                        if abs(l - rec_l) > 10:
                            continue
                        matching_ids.append(str(row['product_id']))
                cursor2.close()

                # Store in Redis cache (30 min TTL)
                if rcache and matching_ids:
                    rcache.setex(cache_key, 1800, json_mod.dumps(matching_ids))
                    rcache.setex(meas_key, 1800, json_mod.dumps(face_meas))
                    print(f"[ALL-FRAMES] Cache SET for user {user_id}: {len(matching_ids)} matches")
        except Exception:
            pass

    return render_template("all_frames.html", products=products, total_count=total_count, rand_seed=seed, matching_ids=matching_ids, face_meas=face_meas)


@bp.route("/api/frames")
def api_frames():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    seed = request.args.get('seed', 0, type=int)
    facefit = request.args.get('facefit', '', type=str).strip()
    color = request.args.get('color', '', type=str).strip()
    shape = request.args.get('shape', '', type=str).strip()
    price_sort = request.args.get('price_sort', '', type=str).strip()
    facematch_ids = request.args.get('facematch_ids', '', type=str).strip()

    is_filtered = bool(facefit or color or shape or facematch_ids)
    # --- Redis cache (Phase B) ---
    _rc = get_redis()
    _cache_key = None
    if _rc and not facematch_ids:
        _cache_key = f"frames:{page}:{per_page}:{seed}:{facefit}:{color}:{shape}:{price_sort}"
        _cached = _rc.get(_cache_key)
        if _cached:
            import json as _json
            resp = make_response(_cached)
            resp.headers['Content-Type'] = 'application/json'
            resp.headers['X-Cache'] = 'HIT'
            return resp
    # --- end cache check ---

    if is_filtered:
        per_page = request.args.get('per_page', 200, type=int)
        if per_page > 500:
            per_page = 500
    else:
        if per_page > 40:
            per_page = 40
    offset = (page - 1) * per_page

    db = get_db()
    cursor = db.cursor()

    where_clauses = [
        "product_image IS NOT NULL",
        "product_image != ''",
        "product_image != 'NULL'",
        "product_quantity > 0",
        "product_category NOT IN ('Contact Lenses', 'Hearing Aids')"
    ]
    params = []

    if facefit:
        where_clauses.append("LOWER(product_perception_value) = LOWER(%s)")
        params.append(facefit)

    if color:
        where_clauses.append("LOWER(color_filter) = LOWER(%s)")
        params.append(color.strip())

    if shape:
        shape_col_map = {
            'rectangle': 'product_category_rectangle',
            'oval': 'product_category_oval',
            'round': 'product_category_round',
            'square': 'product_category_square',
            'wayfarer': 'product_category_wayfarer',
            'aviator': 'product_category_aviator',
            'cateye': 'product_category_cateye',
            'clubmaster': 'product_category_clubmaster',
            'panto': 'product_category_panto',
            'kids': 'product_category_kids',
            'supra': 'product_category_supra'
        }
        col = shape_col_map.get(shape.lower())
        if col:
            where_clauses.append("{} = 1".format(col))

    if facematch_ids:
        ids = [x.strip() for x in facematch_ids.split(',') if x.strip().isdigit()]
        if ids:
            placeholders = ','.join(['%s'] * len(ids))
            where_clauses.append("product_id IN ({})".format(placeholders))
            params.extend(ids)

    where_sql = " AND ".join(where_clauses)

    # Count query
    count_sql = ("SELECT COUNT(*) as cnt FROM products WHERE " + where_sql
                 + catalogue_site_filter())
    cursor.execute(count_sql, params)
    total = cursor.fetchone()['cnt']

    # Data query
    if price_sort == 'asc':
        order_clause = "ORDER BY CAST(product_special_price AS UNSIGNED) ASC"
    elif price_sort == 'desc':
        order_clause = "ORDER BY CAST(product_special_price AS UNSIGNED) DESC"
    elif is_filtered:
        order_clause = "ORDER BY product_name ASC"
    else:
        order_clause = "ORDER BY RAND(%s)"
        params.append(seed)

    data_sql = "SELECT * FROM products WHERE {} {} {} LIMIT %s OFFSET %s".format(
        where_sql, catalogue_site_filter(), order_clause)
    params.extend([per_page, offset])
    cursor.execute(data_sql, params)
    products = cursor.fetchall()
    cursor.close()

    items = []
    for p in products:
        disc_pct = 0
        try:
            pr = int(p['product_price'])
            sp = int(p['product_special_price'])
            if pr > 0:
                disc_pct = round(((pr - sp) / pr) * 100)
        except (ValueError, TypeError, ZeroDivisionError):
            pass

        cat_slug = (p['product_category'] or '').lower().replace(' ', '-')

        items.append({
            'product_id': p['product_id'],
            'product_name': p['product_name'],
            'product_code': p['product_code'],
            'product_price': str(p['product_price']),
            'product_special_price': str(p['product_special_price']),
            'product_category': p['product_category'] or '',
            'product_color': p['product_color'] or '',
            'product_size': p['product_size'] or '',
            'product_slug': p['product_slug'] or '',
            'product_perception_value': p['product_perception_value'] or '',
            'product_primary_color': p.get('product_primary_color') or '',
            'color_filter': p.get('color_filter') or '',
            'color_display': p.get('color_display') or '',
            'product_image': p['product_image'] or '',
            'media': build_media_list(p['product_image'] or ''),
            'discount_pct': disc_pct,
            'product_url': '/categories/{}/{}?pid={}'.format(cat_slug, p['product_slug'], p['product_id']),
            'product_price_eur': str(p.get('product_price_eur', '')) if p.get('product_price_eur') else '',
            'product_special_price_eur': str(p.get('product_special_price_eur', '')) if p.get('product_special_price_eur') else '',
            'product_category_rectangle': str(p.get('product_category_rectangle', 0) or 0),
            'product_category_oval': str(p.get('product_category_oval', 0) or 0),
            'product_category_round': str(p.get('product_category_round', 0) or 0),
            'product_category_square': str(p.get('product_category_square', 0) or 0),
            'product_category_wayfarer': str(p.get('product_category_wayfarer', 0) or 0),
            'product_category_aviator': str(p.get('product_category_aviator', 0) or 0),
            'product_category_cateye': str(p.get('product_category_cateye', 0) or 0),
            'product_category_clubmaster': str(p.get('product_category_clubmaster', 0) or 0),
            'product_category_panto': str(p.get('product_category_panto', 0) or 0),
            'product_category_kids': str(p.get('product_category_kids', 0) or 0),
            'product_category_supra': str(p.get('product_category_supra', 0) or 0)
        })

    _resp_data = jsonify({
        'products': items,
        'page': page,
        'per_page': per_page,
        'total': total,
        'has_more': offset + per_page < total,
        'media_schema': MEDIA_SCHEMA_VERSION
    })
    # --- Redis cache set (Phase B) ---
    if _rc and _cache_key:
        try:
            _rc.setex(_cache_key, 300, _resp_data.get_data(as_text=True))
        except Exception as _ce:
            print(f'[CACHE] frames set error: {_ce}')
    _resp_data.headers['X-Cache'] = 'MISS'
    return _resp_data


@bp.route('/categories/<path:catchall>')
def categories_redirect(catchall):
    """D3: Redirect URLs with spaces or uppercase to hyphenated lowercase.
    E.g., /categories/Spectacles Frame/product → /categories/spectacles-frame/product"""
    import urllib.parse
    decoded = urllib.parse.unquote(catchall)
    normalized = decoded.lower().replace(' ', '-')
    if normalized != catchall:
        pid = request.args.get('pid', '')
        target = f'/categories/{normalized}'
        if pid:
            target += f'?pid={pid}'
        return redirect(target, code=301)
    return "Page not found", 404

@bp.route('/categories/<category>/<product_slug>')
def product_page(category, product_slug):
    product_id = request.args.get('pid', type=int)
    if not product_id:
        return 'Something is wrong',400
        print('Attempting product_id manual search')

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
      "select * from products where product_id = %s",
      (product_id,)
    )
    product = cursor.fetchone()
    if not product:
        return "Product not found", 404
    # A product this storefront does not sell has no page here, and says so the
    # same way a nonexistent one does: a 403 or a redirect would confirm it
    # exists on the other site, and the .in invariant is that it does not exist.
    if not is_product_allowed(product):
        return "Product not found", 404

    expected_slug = product["product_slug"]
    # D1 fix: guard for NULL/empty slug — return 404 instead of crashing
    if not expected_slug or expected_slug == 'none':
        return "Product not found", 404
    expected_category = (product["product_category"] or "spectacles-frame").lower().replace(" ", "-")
    if product_slug != expected_slug or category != expected_category:
       print('Not Slugified')
       return redirect(
           url_for(
               "main.product_page",
                category=expected_category,
                product_slug=expected_slug,
                pid=product_id
           ),
           code=301
       )

    # A contact lens has a page when the release gate says it is finished, and
    # its own structured data: the frame markup below promises complimentary
    # prescription lenses, answers a question about frame size and carries a
    # HowTo about choosing lens type, none of which is true of a box of lenses.
    lens_jsonld = []
    lens = None
    lens_passport = None
    if is_contact_lens(product):
        # One read for every lens fact this page needs — price, matrix, images,
        # SEO — instead of a query per fact from the template.
        lens, lens_passport = lens_view.load_released(
            cursor, product['product_id'], SITE_COM)
        if not lens:
            return "Product not found", 404
        lens_jsonld = lens_view.jsonld(lens, lens.get('matrix'),
                                       'https://optiwar.com')

    # Log product view
    _host = request.host
    _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    _uid = session.get('user_id', 'anon')
    current_app.logger.info(f"[{_host}] ACTIVITY:PRODUCT_VIEW IP:{_ip} user:{_uid} product:{product.get('product_id','?')} slug:{product_slug}")

    # Fetch reviews for this product
    cursor.execute("""
        SELECT r.review_id, r.reviewer_name, r.rating, r.review_title, r.review_text,
               r.is_verified_purchase, r.date_created, r.site_from
        FROM product_reviews r
        WHERE r.product_id = %s AND r.is_approved = 1
        ORDER BY r.date_created DESC
        LIMIT 50
    """, (product_id,))
    reviews = cursor.fetchall()
    # Calculate aggregate rating
    if reviews:
        total_rating = sum(r['rating'] for r in reviews)
        avg_rating = round(total_rating / len(reviews), 1)
        review_count = len(reviews)
    else:
        avg_rating = 0
        review_count = 0
    cursor.close()

    # Derive "Style" from shape category boolean columns
    _shape_map = [
        ('product_category_rectangle', 'Rectangle'),
        ('product_category_oval', 'Oval'),
        ('product_category_round', 'Round'),
        ('product_category_square', 'Square'),
        ('product_category_wayfarer', 'Wayfarer'),
        ('product_category_aviator', 'Aviator'),
        ('product_category_cateye', 'Cat Eye'),
        ('product_category_clubmaster', 'Clubmaster'),
        ('product_category_panto', 'Panto'),
        ('product_category_kids', 'Kids'),
        ('product_category_supra', 'Supra'),
    ]
    _styles = [name for col, name in _shape_map if product.get(col)]
    if _styles:
        product['product_style'] = ', '.join(_styles)

    # Compute INR and EUR discount percentages for description text replacement
    _inr_disc_pct = 0
    _eur_disc_pct = 0
    try:
        pr = int(product['product_price'])
        sp = int(product['product_special_price'])
        if pr > 0 and pr > sp:
            _inr_disc_pct = round(((pr - sp) / pr) * 100)
    except (ValueError, TypeError, ZeroDivisionError):
        pass
    try:
        pr_eur = float(product.get('product_price_eur') or 0)
        sp_eur = float(product.get('product_special_price_eur') or 0)
        if pr_eur > 0 and pr_eur > sp_eur:
            _eur_disc_pct = round(((pr_eur - sp_eur) / pr_eur) * 100)
    except (ValueError, TypeError, ZeroDivisionError):
        pass

    # Face match: check if logged-in user's face measurements match this product
    # 'matched' | 'not_matched' | 'no_measurements'
    face_match_status = 'no_measurements'
    face_fit_label = ''
    face_decentration = 0.0
    face_meas_data = {}
    if 'user_id' in session:
        try:
            _uid = session['user_id']
            cursor2 = db.cursor()
            cursor2.execute("""
                SELECT pd_far, pd_near, face_width, recommended_diameter,
                       recommended_bridge, recommended_length
                FROM face_measurements WHERE customer_id = %s
                ORDER BY measured_at DESC LIMIT 1
            """, (_uid,))
            _meas = cursor2.fetchone()
            cursor2.close()
            if _meas and _meas['pd_far'] and _meas['face_width']:
                _pd = float(_meas['pd_far'])
                _fw = float(_meas['face_width'])
                _rl = int(_meas['recommended_length'] or 140)
                _rd = int(_meas.get('recommended_diameter') or 50)
                _rb = int(_meas.get('recommended_bridge') or 20)
                face_meas_data = {
                    'pd_far': _pd,
                    'face_width': _fw,
                    'recommended_size': '{}-{}-{}'.format(_rd, _rb, _rl),
                }
                face_match_status = 'not_matched'
                _size = product.get('product_size') or ''
                _parts = str(_size).split('-')
                if len(_parts) >= 3:
                    try:
                        _d = int(_parts[0])
                        _b = int(_parts[1])
                        _l = int(_parts[2])
                        _frame_total = (_d * 2) + _b + 10
                        _width_diff = abs(_frame_total - _fw)
                        _frame_pcd = _d + _b
                        _dec = abs(_frame_pcd - _pd) / 2.0
                        _l_diff = abs(_l - _rl)
                        if _width_diff <= 8 and _dec <= 6 and _l_diff <= 10:
                            face_match_status = 'matched'
                            face_decentration = round(_dec, 1)
                            if _width_diff <= 3 and _dec <= 4:
                                face_fit_label = 'EXCELLENT'
                            elif _width_diff <= 5 and _dec <= 5:
                                face_fit_label = 'VERY GOOD'
                            else:
                                face_fit_label = 'GOOD'
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    # Log face demand (matched/not_matched) for stocking intelligence
    if face_match_status in ('matched', 'not_matched') and 'user_id' in session:
        try:
            _site = 'in.optiwar.com' if _req_is_india() else 'optiwar.com'
            cursor3 = db.cursor()
            cursor3.execute("""
                INSERT INTO face_demand_log
                (customer_id, product_id, product_code, product_size,
                 recommended_size, pd_far, face_width, match_status, fit_label, site)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (session['user_id'], product.get('product_id'),
                  product.get('product_code'), product.get('product_size'),
                  face_meas_data.get('recommended_size', ''),
                  face_meas_data.get('pd_far', 0), face_meas_data.get('face_width', 0),
                  face_match_status, face_fit_label or None, _site))
            db.commit()
            cursor3.close()
        except Exception:
            pass

    return render_template("product_page.html", product=product,
                           reviews=reviews, avg_rating=avg_rating, review_count=review_count,
                           inr_disc_pct=_inr_disc_pct, eur_disc_pct=_eur_disc_pct,
                           face_match_status=face_match_status, face_fit_label=face_fit_label,
                           face_decentration=face_decentration, face_meas_data=face_meas_data,
                           lens_jsonld=lens_jsonld, lens=lens,
                           lens_passport=lens_passport)



@bp.route('/api/review/submit', methods=['POST'])
def submit_review():
    """Submit a product review with bot protection."""
    from flask import jsonify
    import time as _time

    # --- Bot protection ---
    # 1. Honeypot: hidden field that bots fill, humans leave blank
    if request.form.get('website', '').strip():
        return jsonify({"success": True, "message": "Review submitted successfully"})  # fake success

    # 2. Timing check: form must be on page >= 5 seconds
    _form_ts = request.form.get('_ts', type=float) or 0
    if _form_ts and (_time.time() - _form_ts) < 4:
        return jsonify({"error": "Please take a moment before submitting"}), 429

    # 3. Rate limit: max 3 reviews per IP per hour
    _ip = request.remote_addr or '0.0.0.0'
    db = get_db()
    _rl_cur = db.cursor()
    _rl_cur.execute("""
        SELECT COUNT(*) as cnt FROM product_reviews
        WHERE ip_address = %s AND date_created > DATE_SUB(NOW(), INTERVAL 1 HOUR)
    """, (_ip,))
    _rl = _rl_cur.fetchone()
    _rl_cur.close()
    if _rl and _rl['cnt'] >= 3:
        return jsonify({"error": "Too many reviews. Please try again later."}), 429
    # --- End bot protection ---

    product_id = request.form.get('product_id', type=int)
    reviewer_name = request.form.get('reviewer_name', '').strip()
    rating = request.form.get('rating', type=int)
    review_title = request.form.get('review_title', '').strip()
    review_text = request.form.get('review_text', '').strip()

    if not product_id or not reviewer_name or not rating or rating < 1 or rating > 5:
        return jsonify({"error": "Missing required fields"}), 400

    _is_india = _req_is_india()
    site = 'in.optiwar.com' if _is_india else 'optiwar.com'

    # Check if logged in user has purchased this product
    is_verified = 0
    customer_id = None
    reviewer_email = None
    if 'user_id' in session:
        customer_id = session['user_id']
        reviewer_email = session.get('user_email', '')
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT 1 FROM orders WHERE customer_id=%s AND product_id=%s LIMIT 1",
                    (customer_id, product_id))
        if cur.fetchone():
            is_verified = 1
        cur.close()

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO product_reviews (product_id, customer_id, reviewer_name, reviewer_email,
                                     rating, review_title, review_text, is_verified_purchase, site_from, ip_address)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (product_id, customer_id, reviewer_name, reviewer_email,
          rating, review_title or None, review_text or None, is_verified, site, _ip))
    db.commit()
    cur.close()

    # Mark any pending review_requests as completed
    try:
        _rr_cur = db.cursor()
        _rr_cur.execute("""
            UPDATE review_requests SET reviewed_at = NOW()
            WHERE product_id = %s AND customer_email = %s AND reviewed_at IS NULL
        """, (product_id, reviewer_email or ''))
        db.commit()
        _rr_cur.close()
    except Exception:
        pass

    return jsonify({"success": True, "message": "Review submitted successfully"})


@bp.route('/review/<token>')
def review_landing(token):
    """One-click review landing page from email link."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT rr.request_id, rr.product_id, rr.customer_id, rr.customer_email,
               p.product_name, p.product_code, p.product_slug, p.product_category,
               p.product_image, c.customer_name
        FROM review_requests rr
        JOIN products p ON rr.product_id = p.product_id
        JOIN customers c ON rr.customer_id = c.customer_id
        WHERE rr.token = %s AND rr.reviewed_at IS NULL
    """, (token,))
    req = cur.fetchone()
    cur.close()

    if not req:
        return "This review link has expired or already been used.", 404

    # Mark as clicked
    cur2 = db.cursor()
    cur2.execute("UPDATE review_requests SET clicked_at = NOW() WHERE token = %s AND clicked_at IS NULL", (token,))
    db.commit()
    cur2.close()

    # Redirect to product page with review anchor
    category_slug = (req['product_category'] or 'spectacles-frame').lower().replace(' ', '-')
    product_url = url_for('main.product_page',
                          category=category_slug,
                          product_slug=req['product_slug'],
                          pid=req['product_id']) + '#review-form'
    return redirect(product_url)

@bp.route('/api/reviews/<int:product_id>', methods=['GET'])
def get_reviews(product_id):
    """Get approved reviews for a product."""
    from flask import jsonify
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT reviewer_name, rating, review_title, review_text,
               is_verified_purchase, date_created
        FROM product_reviews
        WHERE product_id = %s AND is_approved = 1
        ORDER BY date_created DESC
        LIMIT 50
    """, (product_id,))
    reviews = cur.fetchall()
    cur.close()

    result = []
    for r in reviews:
        result.append({
            "name": r["reviewer_name"],
            "rating": r["rating"],
            "title": r.get("review_title", ""),
            "text": r.get("review_text", ""),
            "verified": bool(r["is_verified_purchase"]),
            "date": r["date_created"].strftime("%Y-%m-%d") if r["date_created"] else ""
        })

    avg = round(sum(r["rating"] for r in result) / len(result), 1) if result else 0
    return jsonify({"reviews": result, "avg_rating": avg, "count": len(result)})


@bp.route('/categories/<category>', methods=['GET'])
def categories(category=None):
    db = get_db()
    cursor = db.cursor()
    query = CATEGORY_QUERIES.get(category)
    if query:
         cursor.execute(query + catalogue_site_filter())
         products = cursor.fetchall()
    else:
       products  = []

    #print(f"Categories print {products}")
    cursor.close()
    return render_template('categories.html',category=category,products=products)


@bp.route('/update_quantity', methods=['POST'])
@bp.route('/update_quantity/<product_id>/<path:rest>', methods=['GET'])
def update_quantity(product_id=None, rest=None):
    if product_id and rest:
        parts = rest.rsplit('/', 1)
        action = parts[-1] if len(parts) >= 1 else 'increase'
        rx_id = request.args.get('rx_id', '')
        cart = session.get('cart', [])
        for item in cart:
            if item['product_id'] == str(product_id):
                old_qty = max(1, int(item.get('order_quantity', 1)))
                if action == 'increase':
                    quantity = old_qty + 1
                elif action == 'decrease':
                    quantity = max(1, old_qty - 1)
                else:
                    quantity = old_qty
                item['order_quantity'] = quantity
                atc = safe_float(item.get('ATC_total', 0))
                stp = safe_float(item.get('server_total_price', 0))
                wcl = safe_float(item.get('ATC_WCL', 0))
                if atc > 0:
                    unit_atc = atc / old_qty
                    item['ATC_total'] = unit_atc * quantity
                elif stp == 0:
                    item['ATC_total'] = safe_float(item.get('product_special_price', 0)) * quantity
                if stp > 0:
                    unit_stp = stp / old_qty
                    item['server_total_price'] = unit_stp * quantity
                if wcl > 0:
                    unit_wcl = wcl / old_qty
                    item['ATC_WCL'] = unit_wcl * quantity
                break
        session['cart'] = cart
        return redirect(url_for('main.checkout_page'))
    product_id = request.form['product_id']
    rx_id = request.form.get('rx_id', '')
    quantity = int(request.form.get('quantity', 1))
    if quantity < 1:
        quantity = 1

    cart = session.get('cart', [])
    for item in cart:
        match = item['product_id'] == product_id
        if rx_id:
            match = match and str(item.get('rx_id', '')) == str(rx_id)
        if match:
            old_qty = max(1, int(item.get('order_quantity', 1)))
            item['order_quantity'] = quantity

            # Recalculate ALL price totals based on quantity change
            # For non-prescription items: ATC_total = unit_price * quantity
            atc = safe_float(item.get('ATC_total', 0))
            stp = safe_float(item.get('server_total_price', 0))
            wcl = safe_float(item.get('ATC_WCL', 0))

            if atc > 0:
                unit_atc = atc / old_qty
                item['ATC_total'] = unit_atc * quantity
            elif stp == 0:
                item['ATC_total'] = safe_float(item.get('product_special_price', 0)) * quantity

            # For prescription items: scale server_total_price by quantity
            if stp > 0:
                unit_stp = stp / old_qty
                item['server_total_price'] = unit_stp * quantity

            # For contact lens items: scale ATC_WCL by quantity
            if wcl > 0:
                unit_wcl = wcl / old_qty
                item['ATC_WCL'] = unit_wcl * quantity

            break

    session['cart'] = cart
    return redirect(url_for('main.checkout_page'))


@bp.route('/remove_from_cart/<product_id>', methods=['POST', 'GET'])
@bp.route('/remove_from_cart/<product_id>/<path:extra>', methods=['POST', 'GET'])
def remove_from_cart(product_id, extra=None):
    rx_id = request.form.get('rx_id') or request.args.get('rx_id')
    cart = session.get('cart', [])

    _host = request.host
    _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    _uid = session.get('user_id', 'anon')
    current_app.logger.info(f"[{_host}] ACTIVITY:REMOVE_FROM_CART IP:{_ip} user:{_uid} product:{product_id} rx_id:{rx_id}")

    if rx_id:
        cart = [
            item for item in cart
            if not (item['product_id'] == product_id and str(item.get('rx_id')) == str(rx_id))
        ]
    else:
        # fallback (not ideal, but keep for non-RX items)
        cart = [
            item for item in cart
            if item['product_id'] != product_id
        ]

    session['cart'] = cart
    # Sync cart removal to DB for cross-device persistence
    if session.get('user_id'):
        if cart:
            save_cart_to_db()
        else:
            clear_cart_in_db()
    return redirect(url_for('main.checkout'))



@bp.route('/calculate_pricing', methods=['POST'])
def calculate_pricing():
       right_pwr = request.json.get('right_pwr')
       right_cyl = request.json.get('right_cyl')
       right_add = request.json.get('right_add')
       left_pwr = request.json.get('left_pwr')
       left_cyl = request.json.get('left_cyl')
       left_add = request.json.get('left_add')

       recommendations = []
       if abs(right_pwr) >= 4 or abs(left_pwr) >=4:
            recommendations.append({'lens_type': '1.60_index', 'price': 1500})
       else:
            recommendations.append({'lens_tyoe': '1.59_PC', 'price': 1000}) 

       return jsonify({'recommendations': recommendations})

'''
@bp.route('/add_prescription', methods=['GET', 'POST'])
def add_prescription():
    product_id = request.form.get('product_id')
    product_price = int(float(request.form.get('product_price', 0)))
    product_name = request.form.get('product_name')


    # Get input values from the form
    right_pwr = float(request.form.get('right_pwr', 0))
    right_cyl = float(request.form.get('right_cyl', 0))
    right_axis = float(request.form.get('right_axis', 0))
    right_add = float(request.form.get('right_add', 0))
    left_pwr = float(request.form.get('left_pwr', 0))
    left_cyl = float(request.form.get('left_cyl', 0))
    left_axis = float(request.form.get('left_axis', 0))
    left_add = float(request.form.get('left_add', 0))
    order_quantity = int(request.form.get('order_quantity', 0))
    print(f"Right Power: {right_pwr}, Left Power: {left_pwr}")
    print(f"Product Price {product_price} (Type: {type(product_price)})")

    # Define power ranges and other select options for the form
    pwr_range = [f"{i/100.0:.2f}" for i in range(0, -600, -25)] + \
                [f"{i/100.0:.2f}" for i in range(-600, -2001, -50)] + \
                [f"{i/100.0:.2f}" for i in range(0, 601, 25)]
    cyl_range = [f"{i/100.0:.2f}" for i in range(0, -600, -25)] + \
                [f"{i/100.0:.2f}" for i in range(-600, -850, -50)]
    axis_range = [str(i) for i in range(0, 181, 5)]  # 0 to 180 in steps of 5
    add_range = [f"{i/100.0:.2f}" for i in range(100, 301, 25)]  # +1.00 to +3.00 in steps of 0.25


    
    cart = session.get('cart', [])
    for item in cart:
        if item['product_id'] == product_id:
             flash('You have already entered a prescription for this product')
             return redirect(url_for('main.checkout'))






    cart.append({
         'product_id': product_id,
         'product_name': product_name ,
         'product_price': product_price,
         'right_pwr': right_pwr,
         'right_cyl': right_cyl,
         'right_axis': right_axis,
         'right_add': right_add,
         'left_pwr': left_pwr,
         'left_cyl': left_cyl,
         'left_axis': left_axis,
         'left_add': left_add,
         'order_quantity': order_quantity,
    })

    print(cart)
    session['cart'] = cart
    
    # Render the template and pass necessary variables
    return render_template('prescription.html',
                           product_id=product_id,
                           product_name=product_name,
                           product_price=product_price,
                           order_quantity=order_quantity,
                           right_pwr=right_pwr,
                           right_cyl=right_cyl,
                           right_axis=right_axis,
                           right_add=right_add,
                           left_pwr=left_pwr,
                           left_cyl=left_cyl,
                           left_axis=left_axis,
                           left_add=left_add,
                           pwr_range=pwr_range,
                           cyl_range=cyl_range,
                           axis_range=axis_range,
                           add_range=add_range,
                           cart=cart, 
                            )



@bp.route('/add_to_cart_with_lenses', methods=['POST'])
def add_to_cart_with_lenses():
    db = get_db()
    cursor = db.cursor()

    product_id = request.form['product_id']
    product_price = request.form['product_price']
    product_name = request.form['product_name']
    right_pwr = request.form['right_pwr']
    right_cyl = request.form['right_cyl']
    right_axis = request.form['right_axis']
    right_add = request.form['right_add']
    left_pwr = request.form['left_pwr']
    left_cyl = request.form['left_cyl']
    left_axis = request.form['left_axis']
    left_add = request.form['left_add']


    right_eye = f"{right_pwr}/{right_cyl}/{right_axis}/{right_add}"
    left_eye = f"{left_pwr}/{left_cyl}/{left_axis}/{left_add}"

    cart = session.get('cart', [])



    try:
        cursor.execute('insert into rx_collector (right_eye, left_eye, product_id) values (%s, %s, %s)', (right_eye, left_eye, product_id))
        db.commit()
        rx_id = cursor.lastrowid
    except  Exception as e:
        db.rollback()
        flash(f'Error inserting to rx_collector: {e}')
        return redirect(url_for('main.index'))

    for item in cart:
        if item['product_id'] == product_id:
           item['order_quantity'] +=1
           item['order_total'] = float(item['product_price']) * item['order_quantity']
  #         item['right_pwr'] == right_pwr
           break
    else:
         cart.append({
         'product_id': product_id,
         'product_name': product_name,
         'product_price': product_price,
         'order_quantity': 1,
         'order_total': product_price * order_quantity,
         'rx_id': rx_id,
         'right_eye' : right_eye,
         'left_eye': left_eye
         })

    session['cart'] = cart

    return redirect(url_for('main.checkout'))
'''



@bp.route('/add_prescription', methods=['GET', 'POST'])
def add_prescription():
    product_id = request.form.get('product_id')
    product_special_price = int(safe_float(request.form.get('product_special_price', 0)))
    product_price = int(safe_float(request.form.get('product_price', 0)))
    product_category = request.form.get('product_category', 'category_not_defined')
    product_name = request.form.get('product_name')
    product_code = request.form.get('product_code')
    #product_code = request.form['product_code']


    cart = session.get('cart', [])
    for item in cart:
        if item['product_id'] == product_id:
             if item.get('right_pwr') or item.get('left_pwr') or item.get('right_cyl') or item.get('left_cyl'):
                   flash('Prescription already exist for this product. Choose another product with lenses')
                   return redirect(url_for('main.checkout'))
             else:
                   cart = [i for i in cart if i['product_id'] != product_id]
                   session['cart'] = cart
                   break

    # Get input values from the form
    right_pwr = safe_float(request.form.get('right_pwr', 0))
    right_cyl = safe_float(request.form.get('right_cyl', 0))
    right_axis = safe_float(request.form.get('right_axis', 0))
    right_add = safe_float(request.form.get('right_add', 0))
    left_pwr = safe_float(request.form.get('left_pwr', 0))
    left_cyl = safe_float(request.form.get('left_cyl', 0))
    left_axis = safe_float(request.form.get('left_axis', 0))
    left_add = safe_float(request.form.get('left_add', 0))
    order_quantity = int(request.form.get('order_quantity', 1))
    print(f"Right Power: {right_pwr}, Left Power: {left_pwr}")
    print(f"Product Price {product_special_price} (Type: {type(product_special_price)})")

    '''
    # Generate lens recommendations based on the entered power
    recommendations = []
    lens_price = 0
    cyl_price_increase = 0
    add_price_increase = 0

    if right_add or left_add:
       add_price_increase = 2000
       recommendations.append({'lens_type': 'Progressive Lenses', 'price': 2000})
    else:
        highest_pwr = max(abs(right_pwr), abs(left_pwr))
        if highest_pwr >= 4:
             recommendations.append({'lens_type': 'Plastic 1.61', 'price': 1500})
        elif highest_pwr >= 2:
             recommendations.append({'lens_type': 'Plastic 1.56', 'price': 1200})
        else:
             recommendations.append({'lens_type': 'Plastic 1.49', 'price': 1000})


    highest_cyl = max(abs(right_cyl), abs(left_cyl))
    if 2 <= highest_cyl < 4:
        cyl_price_increase = 400
        recommendations.append({'lens_type': 'Cylinder Adjustment', 'price': 400})
    elif highest_cyl >= 4:
        cyl_price_increase = 1000
7        recommendations.append({'lens_type': 'Cylinder Adjustment','price':1000})

    lens_price = sum(rec['price'] for rec in recommendations)
    print(f"Product Price {product_special_price} (Type: {type(product_special_price)})")
    print(f"Order quantity {order_quantity} (Type: {type(order_quantity)})")
    print(f"Lens Price {lens_price} (Type: {type(lens_price)})")
    print(f"Cyl Price Increase {cyl_price_increase} (Type: {type(cyl_price_increase)})")
    print(f"Add Price Increase {add_price_increase} (Type: {type(add_price_increase)})")


    total_price = (product_special_price * order_quantity) + lens_price + cyl_price_increase + add_price_increase
    print(f"Total Price {total_price} (Type: {type(total_price)})")
    '''
    # Define power ranges and other select options for the form
    pwr_range = [f"{i/100.0:.2f}" for i in range(0, -600, -25)] + \
                [f"{i/100.0:.2f}" for i in range(-600, -2001, -50)] + \
                [f"{i/100.0:.2f}" for i in range(0, 601, 25)]
    cyl_range = [f"{i/100.0:.2f}" for i in range(0, -600, -25)] + \
                [f"{i/100.0:.2f}" for i in range(-600, -851, -50)] + \
                [f"+{i/100.0:.2f}" for i in range(0, 601, 25)] + \
                [f"+{i/100.0:.2f}" for i in range(600, 801, 50)]

    axis_range = [str(i) for i in range(0, 181, 5)]  # 0 to 180 in steps of 5
    add_range = [f"{i/100.0:.2f}" for i in range(100, 301, 25)]  # +1.00 to +3.00 in steps of 0.25


    cart.append({
         'product_id': product_id,
         'product_name': product_name ,
         'product_special_price': product_special_price,
         'product_price':product_price,
         'product_category': product_category,
         #'lens_price': lens_price,
         #'cyl_price_increase': cyl_price_increase,
         #'add_price_increase': add_price_increase,
         #'total_price': total_price,
         'right_pwr': right_pwr,
         'right_cyl': right_cyl,
         'right_axis': right_axis,
         'right_add': right_add,
         'left_pwr': left_pwr,
         'left_cyl': left_cyl,
         'left_axis': left_axis,
         'left_add': left_add,
         'order_quantity': order_quantity,
    })

    print(cart)
    session['cart'] = cart

    # Render the template and pass necessary variables
    session.modified = True
    return render_template('prescription.html',
                           product_id=product_id,
                           product_name=product_name,
                           product_code=product_code,
                           product_special_price=product_special_price,
                           product_price=product_price,
                           product_category=product_category,
                           order_quantity=order_quantity,
                           right_pwr=right_pwr,
                           right_cyl=right_cyl,
                           right_axis=right_axis,
                           right_add=right_add,
                           left_pwr=left_pwr,
                           left_cyl=left_cyl,
                           left_axis=left_axis,
                           left_add=left_add,
                           pwr_range=pwr_range,
                           cyl_range=cyl_range,
                           axis_range=axis_range,
                           add_range=add_range,
                           #recommendations=recommendations,
                           #total_price=total_price,
                           )

def get_addon_price_map(context=None):
    """Load pricing from JSON config file - single source of truth.
    context: None/'default', 'when_bifocal', or 'when_progressive'
    Returns flat dict of {code: {name, price}} for the given context.
    """
    import json as _json
    pricing_file = "/var/www/flask-optiwar-ow-release-090525/lens_pricing.json"
    try:
        with open(pricing_file, "r") as f:
            config = _json.load(f)
        result = {}
        # Always start with default prices (includes bifocal category for addon_3)
        default = config.get("default", {})
        for category in default.values():
            for code, item in category.items():
                result[code] = {"name": item["name"], "price": item["price"], "price_eur": item.get("price_eur", round((item["price"] + 3000) / 100, 2))}
        # Override with context-specific prices if applicable
        if context and context in config:
            ctx = config[context]
            for category in ctx.values():
                for code, item in category.items():
                    result[code] = {"name": item["name"], "price": item["price"], "price_eur": item.get("price_eur", round((item["price"] + 3000) / 100, 2))}
        return result
    except (FileNotFoundError, _json.JSONDecodeError, KeyError):
        return {
            "arc01": {"name": "Anti-Reflection-Coating", "price": 50},
            "bcm02": {"name": "Blue Anti-Glare Coating", "price": 100},
            "arcmat03": {"name": "Multi-Coated Anti-Glare", "price": 200},
            "opsix1": {"name": "Thin lenses Series", "price": 100},
            "ops672": {"name": "Ultra-Thin Lenses", "price": 350},
            "pcgreystyle": {"name": "Photo-Chromatic Grey", "price": 350},
            "pcbostyle": {"name": "Photo-Chromatic Brown", "price": 650},
            "crkt": {"name": "Biofocal Round Style KT", "price": 250},
            "crd": {"name": "Biofocal D Style Flat-Top", "price": 500},
            "prog01": {"name": "Progressive Lenses", "price": 1000},
            "polarized04": {"name": "Polarized Coating", "price": 800},
        }

ADDON_PRICE_MAP = get_addon_price_map()


@bp.route('/add_to_cart_with_lenses', methods=['POST'])
def add_to_cart_with_lenses():
    try:
        db = get_db()
        cursor = db.cursor()

        # === Product & RX form data ===
        product_id = request.form['product_id']
        product_name = request.form.get('product_name', '')
        product_category = request.form.get('product_category', 'category_not_defined')
        product_code = request.form['product_code']

        product_special_price = int(safe_float(request.form.get('product_special_price', 0)))
        product_price = int(safe_float(request.form.get('product_price', 0)))
        total_price = int(safe_float(request.form.get('total_price', 0)))
        order_quantity = int(request.form.get('order_quantity', 1))

        right_pwr = safe_float(request.form.get('right_pwr', 0))
        left_pwr = safe_float(request.form.get('left_pwr', 0))
        right_cyl = safe_float(request.form.get('right_cyl', 0))
        left_cyl = safe_float(request.form.get('left_cyl', 0))
        right_axis = safe_float(request.form.get('right_axis', 0))
        left_axis = safe_float(request.form.get('left_axis', 0))

        right_add = safe_float(request.form.get('right_add', 0) or 0)
        left_add = safe_float(request.form.get('left_add', 0) or 0)

        lens_price = int(float(request.form.get('lens_price', 0)))
        cyl_price_increase = int(float(request.form.get('cyl_price_increase', 0)))
        add_price_increase = int(float(request.form.get('add_price_increase', 0)))

        addon_1 = request.form.get('add_selections')
        addon_2 = request.form.get('add_selections2')
        addon_3 = request.form.get('addon_3')

        # Determine pricing context based on bifocal/progressive selection
        if addon_3 == 'prog01':
            _pricing_context = 'when_progressive'
        elif addon_3 in ('crkt', 'crd'):
            _pricing_context = 'when_bifocal'
        else:
            _pricing_context = None
        _context_prices = get_addon_price_map(_pricing_context)

        addon_1_dict = _context_prices.get(addon_1, {})
        addon_2_dict = _context_prices.get(addon_2, {})
        addon_3_dict = _context_prices.get(addon_3, {})
  
        _is_eur = not _req_is_india()
        _price_key = 'price_eur' if _is_eur else 'price'

        addon_1_name = addon_1_dict.get('name', '')
        addon_1_price = addon_1_dict.get(_price_key, addon_1_dict.get('price', 0))

        addon_2_name = addon_2_dict.get('name', '')
        addon_2_price = addon_2_dict.get(_price_key, addon_2_dict.get('price', 0))

        addon_3_name = addon_3_dict.get('name', '')
        addon_3_price = addon_3_dict.get(_price_key, addon_3_dict.get('price', 0))


        #selections_1_price = ADDON_PRICE_MAP.get(add_selections, 0)
        #print(f"Selections 1 Price: {selections_1_price}")

        right_eye = f"{right_pwr}/{right_cyl}/{right_axis}/{right_add}"
        left_eye = f"{left_pwr}/{left_cyl}/{left_axis}/{left_add}"

        # === Insert initial RX entry ===
        cursor.execute(
            'INSERT INTO rx_collector (right_eye, left_eye, product_id) VALUES (%s, %s, %s)',
            (right_eye, left_eye, product_id)
        )
        rx_id = cursor.lastrowid
        db.commit()
        current_app.logger.info(f"RX ID generated: {rx_id}")

        # === Lens recommendation logic ===
        recommendations = []
        lens_mrp = 0
        server_lens_price = 0
        server_cyl_price_increase = 0
        server_add_price_increase = 0

        if right_add or left_add:
            server_add_price_increase = 0
            _bif_mrp = 25.99 if _is_eur else 2599
            recommendations.append({'lens_type': 'Biofocal or Progressive Selection', 'price': 0, 'lens_mrp': _bif_mrp, 'super_recommendations': 'Anti-Glare Coated'})
            lens_mrp = _bif_mrp
        else:
            highest_pwr = max(abs(right_pwr), abs(left_pwr))
            if highest_pwr >= 8:
                _hp_price = 4 if _is_eur else 400
                _hp_mrp = 25.99 if _is_eur else 2599
                recommendations.append({'lens_type': 'High Power Surcharge', 'price': _hp_price, 'lens_mrp': _hp_mrp, 'super_recommendations': 'High Power Surcharge'})
                lens_mrp = _hp_mrp
            else:
                _comp_mrp = 12.99 if _is_eur else 1299
                recommendations.append({'lens_type': 'Complimentary Lenses included', 'price': 0, 'lens_mrp': _comp_mrp, 'super_recommendations': 'Complimentary lenses'})
                lens_mrp = _comp_mrp

        # Cylinder price adjustment
        highest_cyl = max(abs(right_cyl), abs(left_cyl))
        if highest_cyl > 4:
            server_cyl_price_increase = 10 if _is_eur else 1000
            recommendations.append({'lens_type': 'Cylinder Adjustment', 'price': server_cyl_price_increase})
        elif highest_cyl >= 2:
            server_cyl_price_increase = 4 if _is_eur else 400
            recommendations.append({'lens_type': 'Cylinder Adjustment', 'price': server_cyl_price_increase})

        server_lens_price = sum(rec['price'] for rec in recommendations)
        print(f"Server Lens Price {server_lens_price}")
        lens_type = [rec['lens_type'] for rec in recommendations]
        recommendation_details = ', '.join([f"{rec['lens_type']})" for rec in recommendations])
        optical_lens_price = server_lens_price - product_special_price +  addon_1_price + addon_2_price + addon_3_price
        spectacle_frame_with_complimentary_price = product_price + lens_mrp

        # === Update RX with recommendation ===
        cursor.execute('UPDATE rx_collector SET recommendations = %s WHERE rx_id = %s',
                       (recommendation_details, rx_id))
        db.commit()
        current_app.logger.info(f"Updated recommendations for RX ID {rx_id}")

        # === Store rx_id in session ===
        session.setdefault('rx_ids', []).append(rx_id)
        session.modified = True

        # === Add to cart ===
        cart = session.get('cart', [])
        item_found = False

        for item in cart:
            if item['product_id'] == product_id:
                item.update({
                    'product_name': product_name,
                    'product_code': product_code,
                    'product_category': product_category,
                    'product_special_price': product_special_price,
                    'product_price': product_price,
                    'full_product_price': product_price + lens_mrp,
                    'total_savings': (product_price + lens_mrp) - server_lens_price,
                    'order_quantity': order_quantity,
                    'lens_price': lens_price,
                    'cyl_price_increase': cyl_price_increase,
                    'add_price_increase': add_price_increase,
                    'addon_1': addon_1,
                    'addon_2': addon_2, 
                    'right_eye': right_eye,
                    'left_eye': left_eye,
                    'server_total_price': server_lens_price + addon_1_price + addon_2_price + product_special_price + addon_3_price,
                    'recommendations': recommendation_details,
                    'lens_mrp': lens_mrp,
                    'optical_lens_price': optical_lens_price,
                    'lens_type': lens_type,
                    'server_cyl_price_increase': server_cyl_price_increase,
                    'server_add_price_increase': server_add_price_increase,
                    'addon_1_name': addon_1_name,
                    'addon_1_price': addon_1_price,
                    'addon_2_name': addon_2_name,
                    'addon_2_price': addon_2_price,
                    'addon_3_name': addon_3_name,
                    'addon_3_price': addon_3_price,
                    'spectacle_frame_with_complimentary_price': spectacle_frame_with_complimentary_price,
                    'rx_id': rx_id
                })
                item_found = True
                break

        if not item_found:
            cart.append({
                'product_id': product_id,
                'product_name': product_name,
                'product_code': product_code,
                'product_category': product_category,
                'product_special_price': product_special_price,
                'product_price': product_price,
                'order_quantity': 1,
                'order_total': product_special_price * order_quantity,
                'total_price': total_price,
                'rx_id': rx_id,
                'right_eye': right_eye,
                'left_eye': left_eye,
                'lens_price': server_lens_price,
                'cyl_price_increase': cyl_price_increase,
                'add_price_increase': add_price_increase,
                'server_total_price': server_lens_price + addon_1_price + addon_2_price + product_special_price + addon_3_price,
                'addon_1': addon_1,
                'addon_2': addon_2, 
                'recommendations': recommendation_details,
                'lens_mrp': lens_mrp,
                'full_product_price': product_price + lens_mrp,
                'total_savings': (product_price + lens_mrp) - server_lens_price,
                'optical_lens_price': optical_lens_price,
                'lens_type': lens_type,
                'server_cyl_price_increase': server_cyl_price_increase,
                'addon_1_name': addon_1_name,
                'addon_1_price': addon_1_price,
                'addon_2_name': addon_2_name,
                'addon_2_price': addon_2_price,
                'addon_3_name': addon_3_name,
                'addon_3_price': addon_3_price,
                'spectacle_frame_with_complimentary_price': spectacle_frame_with_complimentary_price,
                'server_add_price_increase': server_add_price_increase
            })

        session['cart'] = cart
        session.modified = True

        current_app.logger.info("🛒 Cart updated and redirecting to checkout")
        return redirect(url_for('main.checkout'))

    except Exception as e:
        import traceback
        traceback.print_exc()
        db.rollback()
        current_app.logger.error(f"❌ Error in add_to_cart_with_lenses: {str(e)}", exc_info=True)
        flash('An error occurred while adding lenses. Please try again.', 'error')
        return redirect(url_for('main.index'))




@bp.route('/delete_address/<int:address_id>', methods=['POST', 'GET'])
def delete_address(address_id):
    user_id = session.get('user_id')
    user_email = session.get('user_email')
    if not user_id and not user_email:
        return jsonify({'status': 'error', 'message': 'Not logged in'}), 401
    try:
        db_conn = get_db()
        cursor = db_conn.cursor()
        if user_id:
            cursor.execute(
                "DELETE FROM customers_address WHERE address_id = %s AND customer_id = %s",
                (address_id, user_id)
            )
        elif user_email:
            cursor.execute(
                "DELETE FROM customers_address WHERE address_id = %s AND customer_id IN "
                "(SELECT customer_id FROM customers WHERE customer_email = %s)",
                (address_id, user_email)
            )
        db_conn.commit()
        cursor.close()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'ok', 'deleted': address_id})
        return redirect(url_for('main.checkout_page'))
    except Exception as e:
        print(f"[DELETE_ADDRESS] Error: {e}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': str(e)}), 500
        return redirect(url_for('main.checkout_page'))


GOOGLE_MAPS_API_KEY = os.environ.get('GOOGLE_MAPS_API_KEY', '')

@bp.route('/api/places_autocomplete')
def places_autocomplete():
    q = request.args.get('input', '')
    if not q or len(q) < 2:
        return jsonify({'predictions': []})
    try:
        _is_india = _req_is_india()
        params = {
            'input': q, 'key': GOOGLE_MAPS_API_KEY,
            'language': 'en'
        }
        if _is_india:
            params['components'] = 'country:in'
        r = http_requests.get('https://maps.googleapis.com/maps/api/place/autocomplete/json', params=params, timeout=5)
        data = r.json()
        if not _is_india and 'predictions' in data:
            data['predictions'] = [p for p in data['predictions']
                if not any(term.get('value', '') in ('India', 'United States', 'USA', 'US')
                    for term in p.get('terms', []))]
        return jsonify(data)
    except Exception:
        return jsonify({'predictions': []})

@bp.route('/api/place_details')
def place_details():
    place_id = request.args.get('place_id', '')
    if not place_id:
        return jsonify({'result': {}})
    try:
        r = http_requests.get('https://maps.googleapis.com/maps/api/place/details/json', params={
            'place_id': place_id, 'key': GOOGLE_MAPS_API_KEY,
            'fields': 'address_components,formatted_address'
        }, timeout=5)
        return jsonify(r.json())
    except Exception:
        return jsonify({'result': {}})


@bp.route('/checkout', methods=['GET'])
def checkout_page():

    # Require login before checkout
    if not session.get('user_id') and not session.get('user_email'):
        flash('Please sign in to proceed with checkout. Your cart is saved.')
        return redirect(url_for('auth.login', next=url_for('main.checkout_page')))

    cart = [item for item in session.get('cart', []) if item.get('product_id') is not None]
    # --- Auto-correct prices for current site (EUR vs INR) ---
    _is_eur_site = not _req_is_india()
    if cart:
        _pids = [item['product_id'] for item in cart if item.get('product_id')]
        if _pids:
            _db = get_db()
            _cur = _db.cursor()
            _placeholders = ','.join(['%s'] * len(_pids))
            _cur.execute(f"SELECT product_id, product_special_price, product_special_price_eur FROM products WHERE product_id IN ({_placeholders})", _pids)
            _price_map = {}
            for _row in _cur.fetchall():
                _price_map[str(_row['product_id'])] = _row
            for item in cart:
                _pid = str(item.get('product_id', ''))
                if _pid in _price_map:
                    _pr = _price_map[_pid]
                    _correct_price = float(_pr.get('product_special_price_eur') or 0) if _is_eur_site else float(_pr.get('product_special_price') or 0)
                    if _correct_price > 0:
                        _old_price = float(item.get('product_special_price', 0))
                        if abs(_old_price - _correct_price) > 0.01:
                            item['product_special_price'] = _correct_price
                            _qty = int(item.get('order_quantity', 1))
                            if 'ATC_total' in item:
                                item['ATC_total'] = _correct_price * _qty
                            if 'server_total_price' in item:
                                _lens = float(item.get('lens_price', 0))
                                _a1 = float(item.get('addon_1_price', 0))
                                _a2 = float(item.get('addon_2_price', 0))
                                _a3 = float(item.get('addon_3_price', 0))
                                item['server_total_price'] = _lens + _a1 + _a2 + _correct_price + _a3
                            if 'ATC_WCL' in item:
                                _rq = float(item.get('right_qty', 0) or 0)
                                _lq = float(item.get('left_qty', 0) or 0)
                                if _rq or _lq:
                                    item['ATC_WCL'] = (_rq + _lq) * _correct_price
            session['cart'] = cart
            session.modified = True
    # --- End price correction ---
    session['cart'] = cart  # Clean up session too
    _host = request.host
    _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    _uid = session.get('user_id', 'anon')
    _cart_count = len(cart)
    _cart_total = sum(item.get('ATC_total', 0) + item.get('server_total_price', 0) + item.get('ATC_WCL', 0) for item in cart)
    current_app.logger.info(f"[{_host}] ACTIVITY:CHECKOUT_VIEW IP:{_ip} user:{_uid} items:{_cart_count} total:{_cart_total}")
    lens_recommendations = []
    print(lens_recommendations)
    for item in cart:
        if 'recommendations' in item:
           lens_recommendations.append(item['recommendations'])

    print(cart)
    grand_total = 0
    for item in cart:
        item_sum = item.get('ATC_total', 0) + item.get('server_total_price', 0) + item.get('ATC_WCL', 0)
        if item_sum == 0:
            # Fallback: item added without pricing fields (e.g. from DB persistence)
            item_sum = safe_float(item.get('product_special_price', 0)) * int(item.get('order_quantity', 1))
        grand_total += item_sum
    # EUR discount: €15 off when 2+ spectacle frames on global site
    _is_eur_ck = not _req_is_india()
    frame_count = sum(1 for item in cart if 'Spectacles Frame' in str(item.get('product_category', '')))
    eur_discount = 15.00 if (_is_eur_ck and frame_count >= 2) else 0
    if eur_discount > 0:
        grand_total = round(grand_total - eur_discount, 2)
        print(f"EUR Discount applied: -€{eur_discount}, frame_count={frame_count}")
    print(f"Grand Total Price: {grand_total}")
    #min_order = 1500
    #if grand_total < min_order:
        #flash(f"Factory Outlet minimum order Rs {min_order}")
        #return redirect(url_for('main.checkout'))

    #current_app.logger.info(f'Grand Total from C_GET: ', {grand_total})

    #grand_total = sum(item['server_total_price'] for item in cart if 'server_total_price' in item)


    
    right_eye = session.get('right_eye', '')
    left_eye = session.get('left_eye', '')
    right_pwr = session.get('right_pwr', 0)
    right_lens_color = session.get('right_lens_color', 0)
    right_cyl = session.get('right_cyl', 0)
    right_axis = session.get('right_axis', 0)
    right_qty = session.get('right_qty', 0)
    right_add = session.get('right_add', '')
    left_pwr = session.get('left_pwr', 0)
    left_lens_color = session.get('left_lens_color', 0)
    left_cyl = session.get('left_cyl', 0)
    left_qty = session.get('left_qty', 0)
    left_axis = session.get('left_axis', 0)
    left_add = session.get('left_add', '')
    cyl_range = []   # 0.00 to -8.00 in steps of 0.25
    axis_range = []               # 0 to 180 in steps of 5
    add_range = []   # +1.00 to +3.00 in steps of 0.25
    ship_days = calculate_ship_date()


    db = get_db()
    cursor = db.cursor()

    product_ids = [item.get('product_id') for item in cart if item.get('product_id')]
    product_image_map = {}

    if product_ids:
       product_ids = [str(pid) for pid in product_ids]
       print(f"Product ID check list: {product_ids}")
       placeholders = ', '.join(['%s'] * len(product_ids))
       sql = f"SELECT product_id, product_image FROM products where product_id IN ({placeholders})"
       cursor.execute(sql, tuple(product_ids))
       rows = cursor.fetchall()
       print(f"Raw product images {rows}")
       for row in rows:
           pid = row['product_id']
           images = row['product_image']
           if images:
                first_image = images.split(',')[0].strip().lstrip('./')
                product_image_map[str(pid)] = first_image
           else:
                product_image_map[pid] = 'default.jpg'

    else:
        print("[DEBUG] Skipping product image because product ids list is empty")

    for k,v in product_image_map.items():
        print(f"  product_id {k} -> image: {v}")
    print(f"Product Image Map: {product_image_map}")

    # Prefill checkout form with saved customer data if logged in
    prefill = {}
    user_id = session.get('user_id')
    user_email = session.get('user_email')
    user_name_sess = session.get('user_name', '')
    print(f"[PREFILL] user_id={user_id}, user_email={user_email}, user_name={user_name_sess}")

    # Use a fresh cursor to avoid any state issues from previous queries
    prefill_cursor = db.cursor()

    if user_id or user_email:
        try:
            cust = None
            if user_id:
                prefill_cursor.execute("SELECT customer_name, customer_email, customer_phone FROM customers WHERE customer_id = %s LIMIT 1", (user_id,))
                cust = prefill_cursor.fetchone()

            if not cust and user_email:
                prefill_cursor.execute("SELECT customer_name, customer_email, customer_phone FROM customers WHERE customer_email = %s ORDER BY customer_id DESC LIMIT 1", (user_email,))
                cust = prefill_cursor.fetchone()

            if cust:
                prefill['name'] = cust.get('customer_name', '') or ''
                prefill['email'] = cust.get('customer_email', '') or ''
                prefill['phone'] = cust.get('customer_phone', '') or ''
                print(f"[PREFILL] Found customer: {prefill.get('name')}, phone: {prefill.get('phone')}, email: {prefill.get('email')}")

            # Session-based fallbacks (always override email with authenticated email)
            if not prefill.get('name') and user_name_sess:
                prefill['name'] = user_name_sess
            if user_email:
                prefill['email'] = user_email  # Always use authenticated email

            # Look for address
            addr = None
            if user_id:
                prefill_cursor.execute("SELECT address, address2, state, zipcode, country FROM customers_address WHERE customer_id = %s ORDER BY address_id DESC LIMIT 1", (user_id,))
                addr = prefill_cursor.fetchone()

            if not addr and user_email:
                prefill_cursor.execute(
                    "SELECT ca.address, ca.address2, ca.state, ca.zipcode, ca.country FROM customers_address ca "
                    "JOIN customers c ON ca.customer_id = c.customer_id "
                    "WHERE c.customer_email = %s ORDER BY ca.address_id DESC LIMIT 1",
                    (user_email,)
                )
                addr = prefill_cursor.fetchone()

            # On optiwar.com, skip India addresses for prefill
            if addr and not _req_is_india() and (addr.get('country') or '').strip().lower() == 'india':
                addr = None
                print(f"[PREFILL] Skipped India address on global site")
            if addr:
                prefill['address'] = addr.get('address', '') or ''
                prefill['address2'] = addr.get('address2', '') or ''
                prefill['state'] = addr.get('state', '') or ''
                prefill['zipcode'] = addr.get('zipcode', '') or ''
                prefill['country'] = addr.get('country', '') or ''
                print(f"[PREFILL] Found address: {prefill.get('address')}")
            else:
                print(f"[PREFILL] No address found for user_id={user_id}, email={user_email}")
        except Exception as e:
            import traceback
            print(f"[PREFILL] Error: {e}")
            traceback.print_exc()

    # Fetch all saved addresses for address selector
    saved_addresses = []
    if user_id or user_email:
        try:
            if user_id:
                prefill_cursor.execute("SELECT address_id, address, address2, state, zipcode, country FROM customers_address WHERE customer_id = %s ORDER BY address_id DESC", (user_id,))
                saved_addresses = list(prefill_cursor.fetchall())
            if not saved_addresses and user_email:
                prefill_cursor.execute(
                    "SELECT ca.address_id, ca.address, ca.address2, ca.state, ca.zipcode, ca.country FROM customers_address ca "
                    "JOIN customers c ON ca.customer_id = c.customer_id "
                    "WHERE c.customer_email = %s ORDER BY ca.address_id DESC",
                    (user_email,)
                )
                saved_addresses = list(prefill_cursor.fetchall())
        except Exception as e:
            print(f"[SAVED_ADDR] Error: {e}")

    prefill_cursor.close()
    print(f"[PREFILL] Final prefill dict: {prefill}")
    # Filter out India addresses on optiwar.com (global/EUR site)
    if saved_addresses and not _req_is_india():
        saved_addresses = [a for a in saved_addresses if (a.get('country') or '').strip().lower() != 'india']
    print(f"[PREFILL] Saved addresses count: {len(saved_addresses)}")

    import uuid as _uuid
    session['checkout_token'] = str(_uuid.uuid4())
    return render_template('checkout.html', cart=cart,ship_days=ship_days,product_image_map=product_image_map, grand_total=grand_total, grand_total_eur=grand_total, eur_discount=eur_discount, subtotal_eur=grand_total + eur_discount, right_eye=right_eye, left_eye=left_eye,right_pwr=right_pwr, right_lens_color=right_lens_color, right_cyl=right_cyl, right_qty=right_qty, right_axis=right_axis, right_add=right_add, left_pwr=left_pwr, left_lens_color=left_lens_color, left_cyl=left_cyl, left_qty=left_qty, left_axis=left_axis, left_add=left_add, lens_recommendations=lens_recommendations, prefill=prefill, saved_addresses=saved_addresses)

"""
@bp.route('/initiate-payment', methods=['POST'])
def initiate_payment_route():
    data = request.json
    customer_name = data.get('customer_name')
    customer_address = data.get('customer_address')
    customer_address2 = data.get('customer_address2', '')
    customer_phone = data.get('customer_phone')
    customer_email = data.get('customer_email')
    customer_state = data.get('customer_state')
    customer_postcode = data.get('customer_postcode')
    country = data.get('country')

    cart = session.get('cart', [])
    if not cart:
        return jsonify({"error": "Your cart is empty."}), 400

    db = get_db()
    cursor = db.cursor()

    try:
        db.begin()
        cursor.execute(
            'INSERT INTO customers (customer_name, customer_phone, customer_email) VALUES (%s, %s, %s)',
            (customer_name, customer_phone, customer_email))
        customer_id = cursor.lastrowid
        print(f'Inserted customer with ID: {customer_id}')
        cursor.execute(
            'INSERT INTO customers_address(customer_id, address, address2, state, zipcode, country) values (%s, %s, %s, %s, %s, %s)',
            (customer_id, customer_address, customer_address2, customer_state, customer_postcode, country))

        cursor.execute('SELECT generate_random_order_id()')
        order_id_result = cursor.fetchone()
        if not order_id_result:
            return jsonify({"error": "Failed to generate order ID."}), 500

        order_id = order_id_result['generate_random_order_id()']
        total_amount = sum(float(item['order_total']) for item in cart)
        callbackurl = current_app.config['PAYTM_CALLBACK_URL']

        # Call the initiate_payment function to get the payment response
        payment_response = initiate_payment(
            order_id=order_id,
            total_amount=total_amount,
            customer_id=customer_id,
            callbackurl=callbackurl
        )

        txn_token = payment_response.get('body', {}).get('txnToken')
        if not txn_token:
            return jsonify({"error": "Payment initiation failed."}), 500

        return jsonify({
            "orderId": order_id,
            "token": txn_token,
            "amount": str(total_amount)
        })

    except Exception as e:
        db.rollback()
        print(f'Error processing payment initiation: {e}')
        return jsonify({"error": str(e)}), 500
"""



@bp.route('/checkout', methods=['POST'])
def checkout():
    # Require login for POST checkout
    if not session.get('user_id') and not session.get('user_email'):
        flash('Please sign in to proceed with checkout.')
        return redirect(url_for('auth.login', next=url_for('main.checkout_page')))
    cart = session.get('cart', [])
    if not cart:
        flash('Your cart is empty.')
        return redirect(url_for('main.index'))

    grand_total = 0
    for item in cart:
        item_sum = item.get('ATC_total', 0) + item.get('server_total_price', 0) + item.get('ATC_WCL', 0)
        if item_sum == 0:
            # Fallback: item added without pricing fields (e.g. from DB persistence)
            item_sum = safe_float(item.get('product_special_price', 0)) * int(item.get('order_quantity', 1))
        grand_total += item_sum
    _host = request.host
    _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    _uid = session.get('user_id', 'anon')
    current_app.logger.info(f"[{_host}] ACTIVITY:CHECKOUT_SUBMIT IP:{_ip} user:{_uid} items:{len(cart)} grand_total:{grand_total}")
    _is_india = _req_is_india()
    if _is_india:
        min_order = 100
        if grand_total < min_order:
           flash(f"Factory outlet minimum total Rs {min_order}. Add more items checkout")
           return redirect(url_for('main.checkout_page'))

    # EUR discount: €15 off when 2+ spectacle frames on global site
    frame_count = sum(1 for item in cart if 'Spectacles Frame' in str(item.get('product_category', '')))
    eur_discount = 15.00 if (not _is_india and frame_count >= 2) else 0
    if eur_discount > 0:
        grand_total = round(grand_total - eur_discount, 2)
        print(f"EUR Discount applied: -EUR {eur_discount}, frame_count={frame_count}")

    # Collect customer info
    customer_name = request.form['name']
    customer_address = request.form['address']
    customer_address2 = request.form.get('address2', '')
    customer_phone = request.form['phone']
    customer_email = request.form['email']
    customer_state = request.form['customer_state']
    customer_postcode = request.form['customer_postcode']
    country = request.form['country']
    phone_code = request.form.get('phone_code', '')
    # Build full delivery phone with country code
    delivery_phone_full = customer_phone if customer_phone.strip().startswith('+') else ((phone_code + ' ' + customer_phone).strip() if phone_code else customer_phone)
    client_ip = request.headers.get('X-Forwarded-For', request.headers.get('X-Real-IP', request.remote_addr))
    if client_ip and ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()

    db = get_db()
    cursor = db.cursor()

    # Checkout idempotency: bind this submission to a one-time token so a
    # double-submit reuses the same order instead of creating a second one.
    import uuid as _uuid
    _checkout_token = session.get('checkout_token') or str(_uuid.uuid4())
    session['checkout_token'] = _checkout_token
    _reuse_order_id = None
    try:
        cursor.execute(
            "INSERT INTO order_intents (token, customer_id, status) VALUES (%s, %s, 'processing')",
            (_checkout_token, session.get('user_id'))
        )
        db.commit()
    except MySQLdb.IntegrityError:
        db.rollback()
        cursor.execute("SELECT order_id FROM order_intents WHERE token=%s", (_checkout_token,))
        _existing_intent = cursor.fetchone()
        if _existing_intent and _existing_intent.get('order_id'):
            _reuse_order_id = _existing_intent['order_id']
            current_app.logger.info(f"[{request.host}] ACTIVITY:CHECKOUT_IDEMPOTENT_REUSE token:{_checkout_token} order:{_reuse_order_id}")
        else:
            flash('Your order is already being processed. Please wait a moment.')
            return redirect(url_for('main.checkout_page'))

    try:
        db.begin()

        # Save/update customer and address
        user_id = session.get('user_id')
        if user_id:
            # Logged-in user: update name and phone only (do NOT overwrite login email)
            cursor.execute(
                'UPDATE customers SET customer_name=%s, customer_phone=%s WHERE customer_id=%s',
                (customer_name, customer_phone, user_id)
            )
            customer_id = user_id
            # Check if this exact address already exists for this user
            cursor.execute(
                'SELECT address_id FROM customers_address WHERE customer_id=%s AND address=%s AND zipcode=%s LIMIT 1',
                (user_id, customer_address, customer_postcode)

            )
            existing_exact = cursor.fetchone()
            if existing_exact:
                # Update the matching address (state/country/delivery contact may have changed)
                cursor.execute(
                    'UPDATE customers_address SET address2=%s, state=%s, country=%s, delivery_phone=%s, delivery_email=%s WHERE address_id=%s',
                    (customer_address2, customer_state, country, delivery_phone_full, customer_email, existing_exact['address_id'])
                )
                delivery_address_id = existing_exact['address_id']
            else:
                # New address - add it to the user's profile
                cursor.execute(
                    'INSERT INTO customers_address (customer_id, address, address2, state, zipcode, country, delivery_phone, delivery_email) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                    (user_id, customer_address, customer_address2, customer_state, customer_postcode, country, delivery_phone_full, customer_email)
                )
                delivery_address_id = cursor.lastrowid
            current_app.logger.info(f'Saved address for logged-in user ID: {customer_id}')
        else:
            # Guest user: insert new customer and address
            cursor.execute(
                'INSERT INTO customers (customer_name, customer_phone, customer_email) VALUES (%s, %s, %s)',
                (customer_name, customer_phone, customer_email)
            )
            customer_id = cursor.lastrowid
            cursor.execute(
                'INSERT INTO customers_address (customer_id, address, address2, state, zipcode, country, delivery_phone, delivery_email) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (customer_id, customer_address, customer_address2, customer_state, customer_postcode, country, delivery_phone_full, customer_email)
            )
            delivery_address_id = cursor.lastrowid
            current_app.logger.info(f'Inserted new guest customer ID: {customer_id}')

        # Generate order ID (or reuse the one bound to this checkout token — idempotency)
        if _reuse_order_id:
            order_id = _reuse_order_id
            current_app.logger.info(f'🧾 Reusing order ID for idempotent checkout: {order_id}')
        else:
            cursor.execute('SELECT generate_random_order_id()')
            order_id_result = cursor.fetchone()
            if not order_id_result:
                raise Exception("Failed to generate order ID.")
            order_id = order_id_result['generate_random_order_id()']
            current_app.logger.info(f'🧾 Generated order ID: {order_id}')
        # Get profile/login email for CC (may differ from delivery email)
        _profile_email = None
        if user_id:
            cursor.execute('SELECT customer_email FROM customers WHERE customer_id=%s', (user_id,))
            _pe_row = cursor.fetchone()
            if _pe_row:
                _profile_email = _pe_row['customer_email']

        # EWS: Notify payment attempted
        try:
            _ews_phone = customer_phone
            _ews_phone_code = request.form.get('phone_code', '')
            if _ews_phone_code and _ews_phone and not _ews_phone.startswith('+'):
                _ews_phone = _ews_phone_code.replace('+','') + _ews_phone
            _ews_currency = '₹' if _req_is_india() else '€'
            notify_payment_attempted(customer_email, _ews_phone, order_id, grand_total, _ews_currency, request.host, profile_email=_profile_email)
        except Exception as _ews_err:
            current_app.logger.error(f"EWS:ERROR payment_attempted {_ews_err}")
        total_amount = grand_total

        _is_optiwarin = 'optiwar.in' in request.host.lower()
        if _is_india and not _is_optiwarin:
            # Paytm for legacy India host (in.optiwar.com)
            callbackurl = url_for('main.payment_callback', _external=True)
            payment_response = initiate_payment(order_id=order_id, customer_id=customer_id, total_amount=total_amount,callbackurl=callbackurl)
            current_app.logger.info(f"Payment response: {payment_response}")
            txn_token = payment_response['body']['txnToken']
            payment_status = verify_payment_status(order_id)
            print(f"{verify_payment_status}")
            razorpay_order_id = None
        else:
            # Razorpay: INR for optiwar.in, EUR for global optiwar.com
            txn_token = None
            _rzp_currency = 'INR' if _is_india else 'EUR'
            rzp_order = create_razorpay_order(order_id=order_id, amount_eur=float(total_amount), currency=_rzp_currency)
            razorpay_order_id = rzp_order['id']
            current_app.logger.info(f"Razorpay order: {razorpay_order_id} for {_rzp_currency} {total_amount}")


        product_ids = [item.get('product_id') for item in cart]
        product_image_map = {}
        placeholders = ', '.join(['%s'] * len(product_ids))
        sql = f"SELECT product_id, product_image FROM products where product_id IN ({placeholders})"
        cursor.execute(sql, tuple(product_ids))
        rows = cursor.fetchall()
        for row in rows:
            pid = row['product_id']
            images = row['product_image']
            first_image = images.split(',')[0].strip().lstrip('./') if images else ''
            product_image_map[str(pid)] = first_image


        valid_rx_count = 0
        missing_rx_items = []

        for idx, item in enumerate(cart, start=1):
            if _reuse_order_id:
                break
            product_id = item.get('product_id')
            order_quantity = item.get('order_quantity')
            order_total = item.get('ATC_total') or item.get('server_total_price') or item.get('ATC_WCL', 0)
            reco_list = []
            if item.get('recommendations'):
                reco_list.append(item['recommendations'])
            if item.get('addon_1_name'):
                reco_list.append(item['addon_1_name'])
            if item.get('addon_2_name'):
                reco_list.append(item['addon_2_name'])
            if item.get('addon_3_name'):
               reco_list.append(item['addon_3_name'])

            recommendations = ', '.join(reco_list) if reco_list else None
            print(f"Recommendations Joined {recommendations}")
            recommendation_price = item.get('optical_lens_price', 0)
            addon_1_name = item.get('addon_1_name')
            addon_1_price = item.get('addon_1_price', 0)
            addon_2_name = item.get('addon_2_name')
            addon_2_price = item.get('addon_2_price', 0)
            addon_3_name = item.get('addon_3_name')
            addon_3_price = item.get('addon_3_price', 0)
            rx_id = item.get('rx_id', None)

            if rx_id:
                cursor.execute(
                    '''
                     UPDATE rx_collector
                     SET recommendations = %s,
                         recommendation_price = %s,
                         addon_1_name = %s,
                         addon_1_price = %s,
                         addon_2_name = %s,
                         addon_2_price = %s,
                         addon_3_name = %s,
                         addon_3_price = %s
                    WHERE rx_id = %s
                    ''',
                    (recommendations, recommendation_price, 
                    addon_1_name, addon_1_price, 
                    addon_2_name, addon_2_price,
                    addon_3_name, addon_3_price,
                    rx_id)
                )
                valid_rx_count += 1
            else:
                missing_rx_items.append(product_id)

            cursor.execute(
                '''
                INSERT INTO orders (order_id, order_quantity, order_total, rx_id, product_id, customer_id, address_id, ip_address, site_from)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''',
                (order_id, order_quantity, order_total, rx_id, product_id, customer_id, delivery_address_id, client_ip, _get_site_from())
            )

            cursor.execute(
                'INSERT INTO order_status (order_status_name, order_id) VALUES (%s, %s)',
                ('Pending', order_id)
            )

        if not _reuse_order_id:
            cursor.execute("UPDATE order_intents SET order_id=%s, status='created' WHERE token=%s", (order_id, _checkout_token))
        db.commit()
        current_app.logger.info(f'✅ Order {order_id} committed successfully.')
        '''
        # 📨 Send email confirmation
        try:
            print(cart)
            order_details = "\n".join([
                f"{item['product_name']} - QTY: {item['order_quantity']} - Total: Rs {item.get('ATC_total', item.get('server_total_price', item.get('ATC_WCL')))}"
                for item in cart
            ])
            send_order_confirmation(
                to_email=customer_email,
                to_cc='admin@lensbazaar.com',
                order_id=order_id,
                customer_name=customer_name,
                cart=cart
            )
            current_app.logger.info(f"📩 Sent order confirmation to {customer_email}")
        except Exception as e:
            current_app.logger.error(f"📧 Error sending mail: {e}")
            flash(f'Order placed but mail not sent to {customer_email}, you may contact customer care 80100 77770 to verify')

        # Clear session
        session.pop('rx_ids', None)
        session.pop('cart', None)

        return redirect(url_for('main.success', order_id=order_id))
        '''

        # Build prefill from submitted form data so fields show behind payment overlay
        post_prefill = {
            'name': customer_name,
            'phone': customer_phone,
            'email': customer_email,
            'address': customer_address,
            'address2': customer_address2,
            'state': customer_state,
            'zipcode': customer_postcode,
            'country': country
        }
        # Fetch saved addresses for the address chips
        post_saved_addresses = []
        try:
            cid = user_id or customer_id
            if cid:
                cursor.execute("SELECT address_id, address, address2, state, zipcode, country FROM customers_address WHERE customer_id = %s ORDER BY address_id DESC", (cid,))
                post_saved_addresses = list(cursor.fetchall())
                # Filter out India addresses on optiwar.com (global/EUR site)
                if post_saved_addresses and not _req_is_india():
                    post_saved_addresses = [a for a in post_saved_addresses if (a.get('country') or '').strip().lower() != 'india']
        except Exception:
            pass
        return render_template('checkout.html', order_id=order_id, payment_token=txn_token, razorpay_order_id=razorpay_order_id, razorpay_key_id=current_app.config.get('RAZORPAY_KEY_ID',''), grand_total=grand_total, grand_total_eur=grand_total, eur_discount=eur_discount, subtotal_eur=grand_total + eur_discount, cart=cart, product_image_map=product_image_map, prefill=post_prefill, saved_addresses=post_saved_addresses)

    except Exception as e:
        db.rollback()
        flash(f'❌ Error processing order: {e}')
        current_app.logger.error(f'❌ Checkout error: {e}', exc_info=True)
        return redirect(url_for('main.checkout_page'))






@bp.route('/test-checkout', methods=['POST'])
def test_checkout():
    if not session.get('user_id') and not session.get('user_email'):
        flash('Please sign in to proceed with checkout.')
        return redirect(url_for('auth.login', next=url_for('main.checkout_page')))

    cart = session.get('cart', [])
    if not cart:
        flash('Your cart is empty.')
        return redirect(url_for('main.index'))

    grand_total = 0
    for item in cart:
        item_sum = item.get('ATC_total', 0) + item.get('server_total_price', 0) + item.get('ATC_WCL', 0)
        if item_sum == 0:
            # Fallback: item added without pricing fields (e.g. from DB persistence)
            item_sum = safe_float(item.get('product_special_price', 0)) * int(item.get('order_quantity', 1))
        grand_total += item_sum
    print(f"[TEST-CHECKOUT] Grand Total: {grand_total}")

    customer_name = request.form['name']
    customer_address = request.form['address']
    customer_address2 = request.form.get('address2', '')
    customer_phone = request.form['phone']
    customer_email = request.form['email']
    customer_state = request.form['customer_state']
    customer_postcode = request.form['customer_postcode']
    country = request.form['country']
    phone_code = request.form.get('phone_code', '')
    delivery_phone_full = customer_phone if customer_phone.strip().startswith('+') else ((phone_code + ' ' + customer_phone).strip() if phone_code else customer_phone)
    client_ip = request.headers.get('X-Forwarded-For', request.headers.get('X-Real-IP', request.remote_addr))
    if client_ip and ',' in client_ip:
        client_ip = client_ip.split(',')[0].strip()

    db = get_db()
    cursor = db.cursor()

    try:
        db.begin()

        user_id = session.get('user_id')
        if user_id:
            cursor.execute(
                'UPDATE customers SET customer_name=%s, customer_phone=%s WHERE customer_id=%s',
                (customer_name, customer_phone, user_id)
            )
            customer_id = user_id
            cursor.execute(
                'SELECT address_id FROM customers_address WHERE customer_id=%s AND address=%s AND zipcode=%s LIMIT 1',
                (user_id, customer_address, customer_postcode)
            )
            existing_exact = cursor.fetchone()
            if existing_exact:
                cursor.execute(
                    'UPDATE customers_address SET address2=%s, state=%s, country=%s, delivery_phone=%s, delivery_email=%s WHERE address_id=%s',
                    (customer_address2, customer_state, country, delivery_phone_full, customer_email, existing_exact['address_id'])
                )
                delivery_address_id = existing_exact['address_id']
            else:
                cursor.execute(
                    'INSERT INTO customers_address (customer_id, address, address2, state, zipcode, country, delivery_phone, delivery_email) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                    (user_id, customer_address, customer_address2, customer_state, customer_postcode, country, delivery_phone_full, customer_email)
                )
                delivery_address_id = cursor.lastrowid
        else:
            cursor.execute(
                'INSERT INTO customers (customer_name, customer_phone, customer_email) VALUES (%s, %s, %s)',
                (customer_name, customer_phone, customer_email)
            )
            customer_id = cursor.lastrowid
            cursor.execute(
                'INSERT INTO customers_address (customer_id, address, address2, state, zipcode, country, delivery_phone, delivery_email) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (customer_id, customer_address, customer_address2, customer_state, customer_postcode, country, delivery_phone_full, customer_email)
            )
            delivery_address_id = cursor.lastrowid

        cursor.execute('SELECT generate_random_order_id()')
        order_id_result = cursor.fetchone()
        if not order_id_result:
            raise Exception("Failed to generate order ID.")
        order_id = order_id_result['generate_random_order_id()']
        print(f"[TEST-CHECKOUT] Generated order ID: {order_id}")

        for idx, item in enumerate(cart, start=1):
            product_id = item.get('product_id')
            order_quantity = item.get('order_quantity')
            order_total = item.get('ATC_total') or item.get('server_total_price') or item.get('ATC_WCL', 0)

            reco_list = []
            if item.get('recommendations'):
                reco_list.append(item['recommendations'])
            if item.get('addon_1_name'):
                reco_list.append(item['addon_1_name'])
            if item.get('addon_2_name'):
                reco_list.append(item['addon_2_name'])
            if item.get('addon_3_name'):
                reco_list.append(item['addon_3_name'])

            recommendations = ', '.join(reco_list) if reco_list else None
            recommendation_price = item.get('optical_lens_price', 0)
            rx_id = item.get('rx_id', None)

            if rx_id:
                cursor.execute(
                    "UPDATE rx_collector SET recommendations=%s, recommendation_price=%s, "
                    "addon_1_name=%s, addon_1_price=%s, addon_2_name=%s, addon_2_price=%s, "
                    "addon_3_name=%s, addon_3_price=%s WHERE rx_id=%s",
                    (recommendations, recommendation_price,
                     item.get('addon_1_name'), item.get('addon_1_price', 0),
                     item.get('addon_2_name'), item.get('addon_2_price', 0),
                     item.get('addon_3_name'), item.get('addon_3_price', 0), rx_id)
                )

            cursor.execute(
                "INSERT INTO orders (order_id, order_quantity, order_total, rx_id, product_id, customer_id, address_id, ip_address, site_from, is_test) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)",
                (order_id, order_quantity, order_total, rx_id, product_id, customer_id, delivery_address_id, client_ip, _get_site_from())
            )
            cursor.execute(
                "INSERT INTO order_status (order_status_name, order_id) VALUES (%s, %s)",
                ('Processed', order_id)
            )

        import json as _json
        test_dump = _json.dumps({'method': 'TEST_BUY', 'amount': str(grand_total), 'test': True})
        cursor.execute(
            "INSERT INTO payment_collector (order_id, payment_dump, status) VALUES (%s, %s, %s)",
            (order_id, test_dump, 'TXN_SUCCESS')
        )

        db.commit()
        print(f"[TEST-CHECKOUT] Order {order_id} committed as TEST order")

        # Get profile/login email for CC
        _profile_email = None
        if user_id:
            cursor.execute('SELECT customer_email FROM customers WHERE customer_id=%s', (user_id,))
            _pe_row = cursor.fetchone()
            if _pe_row:
                _profile_email = _pe_row['customer_email']

        # EWS: Notify payment success for test order
        try:
            _ews_phone = customer_phone
            _ews_phone_code = request.form.get('phone_code', '')
            if _ews_phone_code and _ews_phone and not _ews_phone.startswith('+'):
                _ews_phone = _ews_phone_code.replace('+','') + _ews_phone
            _ews_currency = '\u20b9' if _req_is_india() else '\u20ac'
            notify_payment_success(customer_email, _ews_phone, order_id, grand_total, _ews_currency, request.host, gateway='TEST_PAY', profile_email=_profile_email)
            notify_order_confirmed(customer_email, _ews_phone, customer_name, order_id, grand_total, _ews_currency, request.host, profile_email=_profile_email)
        except Exception as _ews_err:
            current_app.logger.error(f"EWS:ERROR test_payment_success {_ews_err}")

        session.pop('rx_ids', None)
        session.pop('cart', None)

        return redirect(url_for('main.success', order_id=order_id))

    except Exception as e:
        db.rollback()
        current_app.logger.error(f"[TEST-CHECKOUT] Error: {e}")
        import traceback
        traceback.print_exc()
        flash(f'Test checkout failed: {str(e)}')
        return redirect(url_for('main.checkout_page'))


@bp.route('/payment/callbackurl', methods=['POST'])
def payment_callback():
    try:
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form.to_dict()

        current_app.logger.info(f"Received callback data: {data}")

        received_checksum = data.get('CHECKSUMHASH')
        is_valid_checksum = PaytmChecksum.verifySignature(
            data, current_app.config['PAYTM_MERCHANT_KEY'], received_checksum)

        if is_valid_checksum:
            current_app.logger.info("Checksum is valid")
            order_id = data.get('ORDERID')
            payment_status = data.get('STATUS')

            if payment_status == 'TXN_SUCCESS':
                verify_response = verify_payment_status(order_id)
                current_app.logger.info(f"{verify_response}")

                if verify_response and verify_response.get('body'):
                    txn_status = verify_response.get('body', {}).get('resultInfo', {}).get('resultStatus')
                    if txn_status == 'TXN_SUCCESS':
                        _host = request.host
                        _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
                        _uid = session.get('user_id', 'anon')
                        current_app.logger.info(f"[{_host}] ACTIVITY:PAYMENT_SUCCESS IP:{_ip} user:{_uid} order:{order_id} gateway:paytm")
                        db = get_db()
                        # EWS: Notify payment success (Paytm)
                        try:
                            _ews_cur = db.cursor()
                            _ews_cur.execute("SELECT c.customer_email, c.customer_phone, ca.delivery_email, ca.delivery_phone FROM customers c JOIN orders o ON o.customer_id=c.customer_id LEFT JOIN customers_address ca ON ca.address_id=o.address_id WHERE o.order_id=%s LIMIT 1", (order_id,))
                            _ews_cust = _ews_cur.fetchone()
                            if _ews_cust:
                                _ews_cur.execute("SELECT SUM(order_total) as total FROM orders WHERE order_id=%s", (order_id,))
                                _ews_total_r = _ews_cur.fetchone()
                                _ews_total = _ews_total_r['total'] if _ews_total_r and _ews_total_r['total'] else 0
                                _ews_to = _ews_cust.get('delivery_email') or _ews_cust['customer_email']
                                _ews_profile = _ews_cust['customer_email']
                                notify_payment_success(_ews_to, _ews_cust.get('delivery_phone') or _ews_cust.get('customer_phone',''), order_id, _ews_total, '\u20b9', request.host, 'paytm', profile_email=_ews_profile)
                                notify_order_confirmed(_ews_to, _ews_cust.get('delivery_phone') or _ews_cust.get('customer_phone',''), _ews_cust.get('customer_name','Customer'), order_id, _ews_total, '\u20b9', request.host, profile_email=_ews_profile)
                            _ews_cur.close()
                        except Exception as _ews_err:
                            current_app.logger.error(f"EWS:ERROR payment_success_paytm {_ews_err}")

                        cursor = db.cursor()

                        _paytm_ref = data.get('TXNID') or order_id
                        _paid = apply_paid_order(
                            db, order_id, _paytm_ref, data,
                            currency='INR', site=request.host, gateway='paytm',
                            logger=current_app.logger)
                        if not _paid['applied']:
                            _dup_resp = redirect(url_for('main.success', order_id=order_id))
                            _dup_resp.set_cookie('session', '', expires=0)
                            return _dup_resp
                        current_app.logger.info('Order committed to DB')

                        # Get customer email
                        mail_query = '''
                            SELECT c.customer_name, c.customer_email
                            FROM customers c
                            JOIN orders o ON o.customer_id = c.customer_id
                            WHERE o.order_id = %s;
                        '''
                        cursor.execute(mail_query, (order_id,))
                        mail_result = cursor.fetchone()

                        if mail_result:
                            customer_name = mail_result['customer_name']
                            customer_email = mail_result['customer_email']

                            try:
                                cart = session.get('cart', [])
                                order_details = "\n".join([
                                    f"{item['product_name']} - QTY: {item['order_quantity']} - Total: Rs {item.get('ATC_total', item.get('server_total_price', item.get('ATC_WCL'))) }"
                                    for item in cart
                                ])
                                send_order_confirmation(
                                    to_email=customer_email,
                                    to_cc='admin@lensbazaar.com',
                                    order_id=order_id,
                                    customer_name=customer_name,
                                    cart=cart
                                )
                                current_app.logger.info(f"Sent order confirmation to {customer_email}")
                            except Exception as e:
                                current_app.logger.error(f"Error sending mail: {e}")
                                flash(f'Order placed but mail not sent to {customer_email}')

                        session.pop('cart', None)
                        session.clear()
                        response = redirect(url_for('main.success', order_id=order_id))
                        response.set_cookie('session', '', expires=0)
                        return response
                    else:
                        current_app.logger.info(f"Payment verification failed for ORDERID: {order_id}")
                        flash('Payment failed')
                else:
                    current_app.logger.info(f"Payment verification response is None for ORDERID: {order_id}")
                    flash('Payment verification failed')
            else:
                flash('Payment failed')
        else:
            current_app.logger.info("Invalid checksum")
            flash('Invalid checksum')

    except Exception as e:
        current_app.logger.error(f"Error processing callback: {e}")
        flash(f"Error processing callback: {e}")

    return redirect(url_for('main.checkout_page'))




@bp.route('/razorpay/verify', methods=['POST'])
def razorpay_verify():
    """Verify Razorpay payment after checkout."""
    try:
        data = request.get_json()
        razorpay_payment_id = data.get('razorpay_payment_id')
        razorpay_order_id = data.get('razorpay_order_id')
        razorpay_signature = data.get('razorpay_signature')
        order_id = data.get('order_id')

        if not all([razorpay_payment_id, razorpay_order_id, razorpay_signature, order_id]):
            return jsonify({'status': 'error', 'message': 'Missing payment data'}), 400

        is_valid = verify_razorpay_payment(razorpay_order_id, razorpay_payment_id, razorpay_signature)

        if is_valid:
            current_app.logger.info(f"Razorpay payment verified for order {order_id}")
            db = get_db()
            cursor = db.cursor()

            _paid = apply_paid_order(
                db, order_id, razorpay_payment_id,
                {
                    'razorpay_payment_id': razorpay_payment_id,
                    'razorpay_order_id': razorpay_order_id,
                    'razorpay_signature': razorpay_signature,
                    'gateway': 'razorpay'
                },
                currency='INR' if _req_is_india() else 'EUR',
                site=request.host, gateway='razorpay',
                logger=current_app.logger)
            if not _paid['applied']:
                return jsonify({'status': 'success', 'redirect': url_for('main.success', order_id=order_id)})
            _fulfilled_count = _paid['fulfilled_count']
            _host = request.host
            _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            _uid = session.get('user_id', 'anon')
            current_app.logger.info(f"[{_host}] ACTIVITY:PAYMENT_SUCCESS IP:{_ip} user:{_uid} order:{order_id} gateway:razorpay payment_id:{razorpay_payment_id}")
            # EWS: Notify payment success (Razorpay)
            try:
                _ews_cur2 = cursor
                _ews_cur2.execute("SELECT c.customer_email, c.customer_phone, ca.delivery_email, ca.delivery_phone FROM customers c JOIN orders o ON o.customer_id=c.customer_id LEFT JOIN customers_address ca ON ca.address_id=o.address_id WHERE o.order_id=%s LIMIT 1", (order_id,))
                _ews_cust2 = _ews_cur2.fetchone()
                if _ews_cust2:
                    _ews_cur2.execute("SELECT SUM(order_total) as total FROM orders WHERE order_id=%s", (order_id,))
                    _ews_total_r2 = _ews_cur2.fetchone()
                    _ews_total2 = _ews_total_r2['total'] if _ews_total_r2 and _ews_total_r2['total'] else 0
                    _ews_currency2 = '\u20b9' if _req_is_india() else '\u20ac'
                    _ews_to2 = _ews_cust2.get('delivery_email') or _ews_cust2['customer_email']
                    _ews_profile2 = _ews_cust2['customer_email']
                    notify_payment_success(_ews_to2, _ews_cust2.get('delivery_phone') or _ews_cust2.get('customer_phone',''), order_id, _ews_total2, _ews_currency2, request.host, 'razorpay', profile_email=_ews_profile2)
                    if _fulfilled_count > 0:
                        notify_order_confirmed(_ews_to2, _ews_cust2.get('delivery_phone') or _ews_cust2.get('customer_phone',''), _ews_cust2.get('customer_name','Customer'), order_id, _ews_total2, _ews_currency2, request.host, profile_email=_ews_profile2)
            except Exception as _ews_err:
                current_app.logger.error(f"EWS:ERROR payment_success_razorpay {_ews_err}")

            # Send order confirmation email
            try:
                mail_query = "SELECT c.customer_name, c.customer_email FROM customers c JOIN orders o ON o.customer_id = c.customer_id WHERE o.order_id = %s"
                cursor.execute(mail_query, (order_id,))
                mail_result = cursor.fetchone()
                if mail_result:
                    from .models import send_order_confirmation
                    cart = session.get('cart', [])
                    send_order_confirmation(
                        to_email=mail_result['customer_email'],
                        to_cc='admin@lensbazaar.com',
                        order_id=order_id,
                        customer_name=mail_result['customer_name'],
                        cart=cart
                    )
                    current_app.logger.info(f"Sent order confirmation to {mail_result['customer_email']}")
            except Exception as e:
                current_app.logger.error(f"Email error: {e}")

            session.pop('cart', None)
            session.pop('rx_ids', None)

            return jsonify({'status': 'success', 'redirect': url_for('main.success', order_id=order_id)})
        else:
            current_app.logger.warning(f"Razorpay signature verification failed for order {order_id}")
            # EWS: Notify payment failed (Razorpay)
            try:
                _ews_fdb = get_db()
                _ews_fcur = _ews_fdb.cursor()
                _ews_fcur.execute("SELECT c.customer_email, c.customer_phone, ca.delivery_email, ca.delivery_phone FROM customers c JOIN orders o ON o.customer_id=c.customer_id LEFT JOIN customers_address ca ON ca.address_id=o.address_id WHERE o.order_id=%s LIMIT 1", (order_id,))
                _ews_fcust = _ews_fcur.fetchone()
                if _ews_fcust:
                    _ews_fcur.execute("SELECT SUM(order_total) as total FROM orders WHERE order_id=%s", (order_id,))
                    _ews_ftotal_r = _ews_fcur.fetchone()
                    _ews_ftotal = _ews_ftotal_r['total'] if _ews_ftotal_r and _ews_ftotal_r['total'] else 0
                    _ews_fcurrency = '\u20b9' if _req_is_india() else '\u20ac'
                    _ews_fto = _ews_fcust.get('delivery_email') or _ews_fcust['customer_email']
                    _ews_fprofile = _ews_fcust['customer_email']
                    notify_payment_failed(_ews_fto, _ews_fcust.get('delivery_phone') or _ews_fcust.get('customer_phone',''), order_id, _ews_ftotal, _ews_fcurrency, request.host, 'verification_failed', profile_email=_ews_fprofile)
                _ews_fcur.close()
            except Exception as _ews_err:
                current_app.logger.error(f"EWS:ERROR payment_failed_razorpay {_ews_err}")
            return jsonify({'status': 'error', 'message': 'Payment verification failed'}), 400

    except Exception as e:
        current_app.logger.error(f"Razorpay verify error: {e}", exc_info=True)
        return jsonify({'status': 'error', 'message': str(e)}), 500


@bp.route('/razorpay/webhook', methods=['POST'])
def razorpay_webhook():
    """Apply a Razorpay payment reported out-of-band.

    Refuses anything it cannot prove: an unsigned or wrongly signed delivery, an
    order id it cannot resolve, an order that does not exist, an amount short of
    what the order costs, or a currency the order was never priced in.
    Everything it accepts goes through the same pipeline as the browser
    callback, which is idempotent on payment reference — so Razorpay's retries,
    and a webhook racing the browser, apply once.

    The HTTP status decides whether Razorpay retries. 200 ends delivery and is
    the right answer to anything a retry cannot change: a failed payment, an
    unresolvable reference, a wrong currency. A missing order or a short amount
    answer 500, because those are usually this webhook overtaking the write that
    creates the order or settles its total, and the retry minutes later
    succeeds; 200 there loses the payment silently.
    """
    raw = request.get_data()
    if not verify_razorpay_webhook(raw, request.headers.get('X-Razorpay-Signature', '')):
        current_app.logger.warning(
            "ACTIVITY:RAZORPAY_WEBHOOK_REJECTED reason:bad_signature ip:%s"
            % request.headers.get('X-Forwarded-For', request.remote_addr))
        return jsonify({'status': 'error', 'message': 'invalid signature'}), 400

    try:
        event = json_mod.loads(raw.decode('utf-8'))
    except ValueError:
        return jsonify({'status': 'error', 'message': 'invalid payload'}), 400

    kind = event.get('event', '')
    if kind not in PAID_EVENTS:
        return jsonify({'status': 'ignored', 'event': kind}), 200

    payment, order_id = payment_entity(event)
    payment_id = payment.get('id', '')
    if not order_id or not payment_id:
        current_app.logger.error(
            f"ACTIVITY:RAZORPAY_WEBHOOK_UNMATCHED event:{kind} payment:{payment_id}")
        return jsonify({'status': 'error', 'message': 'no order reference'}), 200
    if payment.get('status') != 'captured':
        return jsonify({'status': 'ignored', 'reason': 'payment not captured'}), 200

    db = get_db()
    cursor = db.cursor()
    expected_minor = order_amount_minor(cursor, order_id)
    if not expected_minor:
        current_app.logger.error(
            f"ACTIVITY:RAZORPAY_WEBHOOK_NO_ORDER order:{order_id} payment:{payment_id}")
        return jsonify({'status': 'error', 'message': 'unknown order'}), 500
    paid_minor = int(payment.get('amount') or 0)
    if paid_minor < expected_minor:
        current_app.logger.error(
            "ACTIVITY:RAZORPAY_WEBHOOK_AMOUNT_MISMATCH order:%s payment:%s paid:%s expected:%s"
            % (order_id, payment_id, paid_minor, expected_minor))
        return jsonify({'status': 'error', 'message': 'amount mismatch'}), 500
    if paid_minor > expected_minor:
        current_app.logger.warning(
            "ACTIVITY:RAZORPAY_WEBHOOK_OVERPAID order:%s payment:%s paid:%s expected:%s"
            % (order_id, payment_id, paid_minor, expected_minor))

    currency = order_currency(cursor, order_id)
    paid_currency = payment.get('currency') or currency
    if paid_currency != currency:
        current_app.logger.error(
            "ACTIVITY:RAZORPAY_WEBHOOK_CURRENCY_MISMATCH order:%s payment:%s paid:%s expected:%s"
            % (order_id, payment_id, paid_currency, currency))
        return jsonify({'status': 'error', 'message': 'currency mismatch'}), 200
    paid = apply_paid_order(
        db, order_id, payment_id, {'gateway': 'razorpay', 'event': kind,
                                   'payment': payment},
        currency=currency, site=request.host, gateway='razorpay',
        source='razorpay-webhook', logger=current_app.logger)
    if not paid['applied']:
        return jsonify({'status': 'success', 'reason': paid['reason']}), 200

    current_app.logger.info(
        "ACTIVITY:PAYMENT_SUCCESS source:webhook order:%s gateway:razorpay payment_id:%s "
        "fulfilled:%s refund_pending:%s"
        % (order_id, payment_id, paid['fulfilled_count'], len(paid['refund_lines'])))
    try:
        cursor.execute(
            "SELECT c.customer_name, c.customer_email, c.customer_phone, "
            "ca.delivery_email, ca.delivery_phone "
            "FROM customers c JOIN orders o ON o.customer_id=c.customer_id "
            "LEFT JOIN customers_address ca ON ca.address_id=o.address_id "
            "WHERE o.order_id=%s LIMIT 1", (order_id,))
        cust = cursor.fetchone()
        if cust and not paid['is_test']:
            to_email = cust.get('delivery_email') or cust['customer_email']
            to_phone = cust.get('delivery_phone') or cust.get('customer_phone', '')
            total = expected_minor / 100.0
            symbol = '\u20b9' if currency == 'INR' else '\u20ac'
            notify_payment_success(to_email, to_phone, order_id, total, symbol,
                                   request.host, 'razorpay',
                                   profile_email=cust['customer_email'])
            if paid['fulfilled_count'] > 0:
                notify_order_confirmed(to_email, to_phone,
                                       cust.get('customer_name', 'Customer'),
                                       order_id, total, symbol, request.host,
                                       profile_email=cust['customer_email'])
    except Exception as exc:
        current_app.logger.error(f"EWS:ERROR payment_success_razorpay_webhook {exc}")

    return jsonify({'status': 'success', 'order_id': order_id,
                    'fulfilled': paid['fulfilled_count'],
                    'refund_pending': len(paid['refund_lines'])}), 200


@bp.route('/success/<order_id>', methods=['GET'])
def success(order_id):

    db = get_db()
    cursor = db.cursor()

    # Object-level authorization: an order confirmation may only be viewed by the
    # customer who owns it. Prevents IDOR enumeration of order_id exposing PII
    # (name/email/phone/address/prescription).
    _uid = session.get('user_id')
    _uemail = session.get('user_email')

    # A payment-link return carries Razorpay's signature over this order's own
    # reference, which authorises this one order and nothing else: a link is paid
    # from whatever browser the shopper has, holding no session or somebody
    # else's — an ops-created order is almost never paid from its own account.
    _paid_link = verify_razorpay_payment_link(order_id, request.args)

    if _paid_link:
        _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        current_app.logger.info(
            f"[{request.host}] ACTIVITY:ORDER_SUCCESS_PAYLINK IP:{_ip} order:{order_id} "
            f"payment:{request.args.get('razorpay_payment_id')}"
        )
    else:
        if not _uid and not _uemail:
            flash('Please sign in to view your order.')
            return redirect(url_for('auth.login', next=request.path))
        cursor.execute(
            "SELECT o.order_id FROM orders o "
            "LEFT JOIN customers c ON c.customer_id = o.customer_id "
            "WHERE o.order_id = %s AND (o.customer_id = %s OR c.customer_email = %s) LIMIT 1",
            (order_id, _uid, _uemail)
        )
        if not cursor.fetchone():
            _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            current_app.logger.warning(
                f"[{request.host}] ACTIVITY:ORDER_SUCCESS_DENIED IP:{_ip} user:{_uid or _uemail} order:{order_id}"
            )
            abort(404)

    try:

       cursor.execute(
          '''select o.order_id, o.date_created, ca.address, ca.address2, ca.state,ca.zipcode, ca.country,
                    ca.delivery_phone, ca.delivery_email,
                    p.product_name, p.product_image, p.product_special_price,p.product_category,product_code, 
                    c.customer_email, c.customer_name, c.customer_phone,
                    o.order_quantity, o.order_total, 
                    rc.right_eye, rc.left_eye, rc.recommendations,
                    rc.addon_1_name, rc.addon_1_price, 
                    rc.addon_2_name, rc.addon_2_price,
                    rc.addon_3_name, rc.addon_3_price,
                    os.order_status_name 
                    from orders o
                    join products p on p.product_id=o.product_id 
                    left join rx_collector rc on rc.rx_id=o.rx_id 
                    left join customers_address ca on ca.address_id=o.address_id
                    left join customers c on c.customer_id=o.customer_id
                    left join order_status os on os.order_id=o.order_id 
                    left join payment_collector pc on pc.order_id=o.order_id
                    where o.order_id= %s
                    group by o.order_id, o.date_created, ca.country, rc.recommendations, rc.right_eye, rc.left_eye, o.order_total
          ''' , (order_id,))


       order_details = normalize_rows(cursor.fetchall())
       #print(f"Fetched order display: {order_details} and Types display")
       _host = request.host
       _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
       _uid = session.get('user_id', 'anon')
       current_app.logger.info(f"[{_host}] ACTIVITY:ORDER_SUCCESS_VIEW IP:{_ip} user:{_uid} order:{order_id}")

       cursor.execute('''select sum(order_total) as grand_total from orders where order_id=%s''', (order_id,))
       grand_total_result = cursor.fetchone() 
       grand_total = int(grand_total_result['grand_total']) if grand_total_result and 'grand_total' in grand_total_result else 0
       print(f"Fetched Grand Total for display type is {type(grand_total)}")

       payment_state, latest_status = order_payment_state(cursor, order_id)


    except Exception as e:
        print(f"Error retreiving your order info: {e}")
        return "There is something wrong, why not contact customer service at +91-8010077770", 500

    ship_date = calculate_ship_date()

    # Google Customer Reviews opt-in fields (order confirmation page).
    gcr = None
    if current_app.config.get('GCR_MERCHANT_ID') and order_details:
        _row = order_details[0]
        _email = _row.get('delivery_email') or _row.get('customer_email') or ''
        _country = country_to_iso2(_row.get('country'))
        # Estimated *delivery* date (ISO YYYY-MM-DD) = dispatch date + transit
        # allowance, so Google schedules the review survey after the parcel is
        # expected to arrive (not on dispatch). Transit differs by destination:
        # domestic India vs. international (shipped from India).
        _transit = (current_app.config['GCR_TRANSIT_DAYS_IN'] if _country == 'IN'
                    else current_app.config['GCR_TRANSIT_DAYS_INTL'])
        _delivery = _add_business_days(dispatch_date_obj(), _transit)
        gcr = {
            'merchant_id': current_app.config['GCR_MERCHANT_ID'],
            'order_id': str(order_id),
            'email': _email,
            'delivery_country': _country,
            'estimated_delivery_date': _delivery.strftime('%Y-%m-%d'),
        }

    if payment_state != 'paid':
        current_app.logger.info(
            f"[{request.host}] ACTIVITY:ORDER_SUCCESS_UNPAID order:{order_id} "
            f"state:{payment_state} status:{latest_status or '-'}")

    return render_template('success.html', order_details=order_details, grand_total=grand_total,
                           ship_date=ship_date, gcr=gcr, payment_state=payment_state,
                           latest_status=latest_status)


@bp.route('/terms_and_conditions')
def terms_and_conditions():
    return render_template('terms-and-conditions.html')


@bp.route('/privacy_policy')
def privacy_policy():
    return render_template('privacy_policy.html')

@bp.route('/clear_the_cart', methods=['POST'])
def clear_the_cart():
    _host = request.host
    _ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    _uid = session.get('user_id', 'anon')
    current_app.logger.info(f"[{_host}] ACTIVITY:CLEAR_CART IP:{_ip} user:{_uid}")
    session.pop('cart', None)
    session.pop('lensRecommendation', None)
    flash('Cart has been cleared.')
    return redirect(url_for('main.index'))



# ============================================================
# SEO/AEO Routes: robots.txt, sitemap.xml, /api/products
# ============================================================

@bp.route('/robots.txt')
def robots_txt():
    """Serve robots.txt for search engine crawlers."""
    _rh = request.host.lower()
    if not _req_is_india():
        _host = 'optiwar.com'
    elif 'optiwar.in' in _rh:
        _host = 'optiwar.in'
    else:
        _host = 'in.optiwar.com'
    robots = f"""User-agent: *
Allow: /
Disallow: /checkout
Disallow: /profile/
Disallow: /auth/
Disallow: /initiate-payment
Disallow: /clear_the_cart
Disallow: /contact_us

Sitemap: https://{_host}/sitemap.xml
Sitemap: https://{_host}/sitemap_index.xml
"""
    response = make_response(robots)
    response.headers['Content-Type'] = 'text/plain'
    return response


def _live_lens_rows(cur):
    """Released lenses with their imagery, [] on .in or if the read fails.

    Shared by the feed and both sitemaps so all three publish the same set: a
    surface that queried the catalogue itself would be free to disagree with the
    release gate, which is the defect the gate exists to prevent.
    """
    if _req_is_india():
        return []
    try:
        rows = live_lenses(cur, SITE_COM)
        for row in rows:
            row['images'] = lens_feed.lens_images(cur, row['product_id'])
        return rows
    except Exception as e:  # noqa: BLE001
        current_app.logger.warning('[SEO] contact lenses omitted: %s', e)
        return []


def _lens_feed_items(cur, base, is_india):
    """Merchant items for every released contact lens, [] on .in.

    A lens catalogue that cannot be read must not take the frame feed with it:
    702 frames disappearing from Merchant Center because a lens table is missing
    would be a far larger outage than the lenses not appearing, so the failure is
    logged and the feed is served without them.
    """
    if is_india:
        return []
    rows = _live_lens_rows(cur)
    return lens_feed.lens_items(rows, base, is_india=False)


# ============================================================
# Google Merchant Center product feed (RSS 2.0 + g: namespace)
# ============================================================
@bp.route('/feed/google-merchant.xml')
def google_merchant_feed():
    """Storefront-aware Google Shopping feed.

    optiwar.com -> EUR, optiwar.in -> INR. image_link is the canonical
    versioned MASTER jpeg (never AVIF/WebP); additional_image_link carries the
    remaining angles (max 10 per Google). Identifiers: no GTIN exists, so we
    send brand + mpn (=product_code); Google treats brand+mpn as a valid
    identifier pair, so identifier_exists is not set to "no". Discontinued rows
    are excluded; out-of-stock rows are kept as availability=out_of_stock."""
    from xml.sax.saxutils import escape as _xesc

    is_india = _req_is_india()
    _rh = request.host.lower()
    if not is_india:
        host, currency, title_suffix = 'optiwar.com', 'EUR', 'Optiwar (Global)'
    elif 'optiwar.in' in _rh:
        host, currency, title_suffix = 'optiwar.in', 'INR', 'Optiwar (India)'
    else:
        host, currency, title_suffix = 'in.optiwar.com', 'INR', 'Optiwar (India)'
    base = 'https://' + host

    # 178 = "Sunglasses" in Google's taxonomy, wrong for prescription eyeglass
    # frames. Default-remove the legacy hardcoded category so Google
    # auto-categorizes; a verified replacement is adopted only after taxonomy +
    # Merchant Center evidence. Flag lets us restore the old value instantly.
    _remove_legacy_cat = os.environ.get(
        'GMC_REMOVE_LEGACY_CATEGORY', 'true').lower() in ('1', 'true', 'yes', 'on')

    db = get_db()
    cur = db.cursor()
    # The query below selects gmc_age_group, so the column has to exist before
    # it runs on a database the migration has not reached.
    ensure_gmc_columns(cur)
    cur.execute("""
        SELECT product_id, product_code, product_name, product_details,
               product_category, product_slug, product_image, product_quantity,
               product_status, product_vertical,
               product_price, product_special_price,
               product_price_eur, product_special_price_eur,
               color_display, product_color, product_material,
               product_country_of_manufacture, product_gender,
               product_diameter, product_bridge, product_lenght, product_size,
               product_category_kids, gmc_age_group,
               product_category_rectangle, product_category_wayfarer,
               product_category_aviator, product_category_cateye,
               product_category_round, product_category_oval,
               product_category_square, product_category_clubmaster,
               product_category_browline, product_category_panto,
               product_category_quatra, product_category_horn,
               product_category_oversized
        FROM products
        WHERE (discontinued = 0 OR discontinued IS NULL)
          AND product_image IS NOT NULL AND product_image != ''
          AND product_slug IS NOT NULL AND product_slug != ''
          """ + catalogue_site_filter() + """
        ORDER BY product_id
    """)
    rows = cur.fetchall()

    items = []
    for p in rows:
        # Feed inclusion policy: ACTIVE + in-stock only (drops OUT_OF_STOCK etc.).
        if not is_merchant_eligible(p):
            continue
        # A lens is not a frame with a different name: everything below emits
        # brand Optiwar and mpn = our own product code, true of a frame we
        # assemble and false of an Alcon box with a real brand, GTIN and MPN.
        # Lenses are appended after this loop, from lens_feed, which maps the
        # manufacturer's identity instead of ours.
        if is_contact_lens(p):
            continue
        if is_india:
            price = p.get('product_special_price') or p.get('product_price')
            mrp = p.get('product_price')
        else:
            price = p.get('product_special_price_eur') or p.get('product_price_eur')
            mrp = p.get('product_price_eur')
        if not price:
            continue

        ordered = versioned_angle_urls(p.get('product_image') or '', base, limit=11)
        if not ordered:
            continue
        image_link = ordered[0]
        additional = ordered[1:11]

        cat = (p.get('product_category') or '').lower().replace(' ', '-')
        link = '%s/categories/%s/%s?pid=%s' % (base, cat, p['product_slug'], p['product_id'])
        try:
            _qty = int(p.get('product_quantity') or 0)
        except (TypeError, ValueError):
            _qty = 0
        avail = 'in_stock' if _qty > 0 else 'out_of_stock'
        title = ('%s %s' % (p.get('product_name') or '', p.get('product_code') or '')).strip()
        color = p.get('color_display') or p.get('product_color') or ''
        shape = frame_shape(p)
        # Clean, currency-free description (raw product_details carries INR/GST
        # text which would mismatch the EUR feed price and risk disapproval).
        _sent = [title]
        _typ = (' '.join(x for x in (shape, p.get('product_category') or 'Spectacles Frame') if x)).strip()
        if color:
            _sent.append('%s in %s.' % (_typ, color))
        else:
            _sent.append('%s.' % _typ)
        if p.get('product_diameter') and p.get('product_bridge') and p.get('product_lenght'):
            _sent.append('Lens %smm, bridge %smm, temple %smm.' % (
                p['product_diameter'], p['product_bridge'], p['product_lenght']))
        if p.get('product_material'):
            _sent.append('%s.' % p['product_material'])
        _sent.append('Includes complimentary prescription lenses and free %s.' % (
            'delivery across India' if is_india else 'worldwide delivery'))
        desc = ' '.join(s for s in _sent if s).strip()

        parts = [
            '    <item>',
            '      <g:id>%s</g:id>' % _xesc(str(p.get('product_code') or p['product_id'])),
            '      <g:title>%s</g:title>' % _xesc(title),
            '      <g:description>%s</g:description>' % _xesc(desc),
            '      <g:link>%s</g:link>' % _xesc(link),
            '      <g:image_link>%s</g:image_link>' % _xesc(image_link),
        ]
        for a in additional:
            if a:
                parts.append('      <g:additional_image_link>%s</g:additional_image_link>' % _xesc(a))
        parts += [
            '      <g:availability>%s</g:availability>' % avail,
            '      <g:price>%s %s</g:price>' % (mrp or price, currency),
        ]
        if p.get('product_special_price' if is_india else 'product_special_price_eur') and mrp and str(mrp) != str(price):
            parts.append('      <g:sale_price>%s %s</g:sale_price>' % (price, currency))
        parts += [
            '      <g:condition>new</g:condition>',
            '      <g:brand>Optiwar</g:brand>',
            '      <g:mpn>%s</g:mpn>' % _xesc(str(p.get('product_code') or '')),
        ]
        if not _remove_legacy_cat:
            parts.append('      <g:google_product_category>178</g:google_product_category>')
        parts += [
            '      <g:product_type>%s</g:product_type>' % _xesc(p.get('product_category') or 'Spectacles Frame'),
        ]
        if color:
            parts.append('      <g:color>%s</g:color>' % _xesc(color))
        if p.get('product_material'):
            parts.append('      <g:material>%s</g:material>' % _xesc(p['product_material']))
        if p.get('product_gender'):
            parts.append('      <g:gender>%s</g:gender>' % _xesc(str(p['product_gender']).lower()))
        # Google demotes an offer whose product type requires age_group and does
        # not carry it — 29 of ours were demoted in DE/FR/GB for exactly this.
        # An ordinary frame is adult; one of the 13 product_category_kids frames
        # is emitted only once somebody has assigned gmc_age_group, and stays
        # demoted until then rather than being called adult to silence Google.
        _age = age_group(p)
        if _age:
            parts.append('      <g:age_group>%s</g:age_group>' % _xesc(_age))
        if shape:
            parts.append('      <g:custom_label_0>%s</g:custom_label_0>' % _xesc(shape))
        parts.append('    </item>')
        items.append('\n'.join(parts))

    items += _lens_feed_items(cur, base, is_india)

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">\n'
        '  <channel>\n'
        '    <title>%s Product Feed</title>\n'
        '    <link>%s/</link>\n'
        '    <description>Optiwar spectacle frames, sunglasses and eyewear.</description>\n'
        '%s\n'
        '  </channel>\n'
        '</rss>\n'
    ) % (_xesc(title_suffix), base, '\n'.join(items))

    response = make_response(xml)
    response.headers['Content-Type'] = 'application/xml; charset=utf-8'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


# ============================================================
# LENS CONTENT PAGES - Static SEO/AEO pages for each lens type
# ============================================================
LENS_DATA = {
    'anti-glare-lenses': {
        'name': 'Anti-Glare Lenses',
        'name_short': 'Anti-Glare Lenses',
        'slug': 'anti-glare-lenses',
        'price': 50,
        'price_label': 'Add-on Rs 50 with any frame',
        'color': '#e8f5e9',
        'icon': '\U0001f7e2',
        'widget_class': 'anti-glare',
        'widget_icon': '\u2726',
        'widget_label': 'Anti-Glare',
        'short_desc': 'Reduce reflections and glare for comfortable all-day wear. Anti-reflection coating improves clarity and appearance.',
        'meta_description': 'Anti-Glare Lenses at Rs 50 add-on with any spectacle frame at Optiwar. Reduces reflections, improves clarity, and enhances your appearance. Complimentary base lenses included. Free shipping India.',
        'keywords': 'anti-glare lenses, anti-reflection coating lenses, AR coated lenses, anti-glare spectacle lenses India, optiwar anti-glare',
        'what_are': 'Anti-glare lenses, also known as anti-reflection (AR) coated lenses, have a special multi-layer coating applied to the lens surface that reduces reflections from the front and back of the lens. When light hits an uncoated lens, about 8% is reflected away, causing glare and reducing visual clarity. AR coating eliminates these reflections, allowing 99.5% of light to pass through for sharper, clearer vision. The coating also makes your lenses nearly invisible, so people can see your eyes clearly without distracting reflections.',
        'benefits': [
            'Eliminates distracting reflections from lens surfaces for clearer vision',
            'Reduces eye strain during prolonged reading or screen use',
            'Makes lenses appear nearly invisible for a better cosmetic look',
            'Improves night driving by cutting headlight glare',
            'Enhances contrast and visual sharpness in all lighting conditions',
            'Reduces halos around lights at night'
        ],
        'who_should_use': 'Anti-glare lenses benefit everyone who wears glasses, but they are especially recommended for people who drive at night, work long hours on computers, attend meetings where reflections can be distracting, or anyone who wants their glasses to look clean and invisible. If you find yourself bothered by reflections in your lenses from overhead lights or when taking photos, anti-glare coating is the solution.',
        'specs': [
            {'label': 'Coating Type', 'value': 'Multi-Layer AR'},
            {'label': 'Light Transmission', 'value': '99.5%'},
            {'label': 'Reflection Reduction', 'value': 'Up to 99%'},
            {'label': 'Add-on Price', 'value': 'Rs 50'}
        ],
        'how_it_works': 'When you purchase any spectacle frame from Optiwar, simply select Anti-Glare coating as an add-on during the prescription step. Your base single-vision lenses are complimentary. The Anti-Glare upgrade costs just Rs 50 and is applied to both lenses. Our system automatically configures the right lens material based on your prescription power.',
        'faqs': [
            {'q': 'Are anti-glare lenses worth it?', 'a': 'Absolutely. At just Rs 50, anti-glare coating dramatically improves visual comfort, reduces eye strain, and makes your glasses look better. It is one of the most cost-effective lens upgrades available.'},
            {'q': 'How long does anti-glare coating last?', 'a': 'With proper care, anti-glare coating lasts 1-2 years. Avoid wiping lenses with rough cloth or paper tissues. Use the microfiber cloth provided with your glasses.'},
            {'q': 'Can anti-glare be added to any prescription?', 'a': 'Yes. Anti-glare coating can be applied to any prescription power, whether you are near-sighted, far-sighted, or have astigmatism.'}
        ]
    },
    'blue-cut-lenses': {
        'name': 'Blue Cut Lenses',
        'name_short': 'Blue Cut Lenses',
        'slug': 'blue-cut-lenses',
        'price': 100,
        'price_label': 'Add-on Rs 100 with any frame',
        'color': '#e3f2fd',
        'icon': '\U0001f535',
        'widget_class': 'blue-cut',
        'widget_icon': '\U0001f499',
        'widget_label': 'Blue Cut',
        'short_desc': 'Block harmful blue light from screens and digital devices. Protect your eyes during long hours of computer and phone use.',
        'meta_description': 'Blue Cut Lenses at Rs 100 add-on at Optiwar. Blocks harmful blue light from computers, phones, and LED screens. Reduces digital eye strain and improves sleep quality. Free shipping India.',
        'keywords': 'blue cut lenses, blue light blocking glasses, blue ray lenses, computer glasses India, anti blue light lenses, optiwar blue cut',
        'what_are': "Blue cut lenses are specially designed lenses with a coating that filters out high-energy blue-violet light (380-450nm wavelength) emitted by digital screens, LED lighting, and sunlight. In today's digital world, we spend an average of 8-10 hours daily looking at screens. This prolonged blue light exposure can cause digital eye strain, headaches, dry eyes, and may disrupt natural sleep patterns by suppressing melatonin production. Blue cut lenses selectively block the harmful blue light spectrum while allowing beneficial light to pass through, keeping your vision natural and comfortable.",
        'benefits': [
            'Blocks up to 95% of harmful blue-violet light (380-450nm)',
            'Significantly reduces digital eye strain and fatigue from screen use',
            'Helps maintain natural sleep cycles by reducing blue light exposure before bed',
            'Reduces headaches caused by prolonged screen time',
            'Includes anti-glare coating for additional reflection reduction',
            'Protects against long-term retinal damage from cumulative blue light exposure'
        ],
        'who_should_use': 'Blue cut lenses are essential for anyone who spends significant time in front of digital screens \u2014 IT professionals, software developers, students, gamers, content creators, and office workers. They are also recommended for children who use tablets and computers for education. If you experience headaches, dry eyes, or trouble sleeping after extended screen use, blue cut lenses can provide significant relief.',
        'specs': [
            {'label': 'Blue Light Blocking', 'value': 'Up to 95%'},
            {'label': 'Wavelength Filtered', 'value': '380-450nm'},
            {'label': 'Includes', 'value': 'AR Coating'},
            {'label': 'Add-on Price', 'value': 'Rs 100'}
        ],
        'how_it_works': 'Select any spectacle frame at Optiwar and choose Blue Cut as your lens upgrade during the prescription step. The add-on costs Rs 100. Base prescription lenses are complimentary with every frame. Blue cut coating is applied to both lenses and includes built-in anti-glare properties. Our AI system selects the optimal lens material for your power automatically.',
        'faqs': [
            {'q': 'Do blue cut lenses have a yellow tint?', 'a': 'Modern blue cut lenses have a very minimal residual tint that is nearly imperceptible in normal use. You may notice a faint blue-purple reflection on the lens surface, which is normal and indicates the coating is active.'},
            {'q': 'Can I use blue cut lenses without a prescription?', 'a': 'Yes. Many people order blue cut lenses with zero power (plano) specifically for screen protection. Simply enter 0.00 as your prescription power during checkout.'},
            {'q': 'Are blue cut lenses the same as anti-glare?', 'a': 'No. Blue cut lenses include anti-glare coating PLUS blue light filtering technology. They offer broader protection than standard anti-glare lenses alone.'}
        ]
    },
    'multi-anti-glare-lenses': {
        'name': 'Multi Anti-Glare Lenses',
        'name_short': 'Multi Anti-Glare Lenses',
        'slug': 'multi-anti-glare-lenses',
        'price': 200,
        'price_label': 'Add-on Rs 200 with any frame',
        'color': '#fce4ec',
        'icon': '\U0001f308',
        'widget_class': 'multi-glare',
        'widget_icon': '\u2728',
        'widget_label': 'Multi-Glare',
        'short_desc': 'Premium multi-coated lenses combining anti-glare, scratch resistance, UV protection, and hydrophobic layers in one advanced coating.',
        'meta_description': 'Multi Anti-Glare Lenses at Rs 200 add-on at Optiwar. Premium multi-coated lens with anti-reflection, scratch resistance, UV protection, and water-repellent coating. Free shipping India.',
        'keywords': 'multi-coated lenses, multi anti-glare lenses, HMC lenses, super hydrophobic lenses India, premium coated lenses, optiwar multi-glare',
        'what_are': 'Multi anti-glare lenses feature an advanced multi-layer coating system that goes beyond standard anti-reflection. Each layer serves a specific function: the base layer provides scratch resistance, the middle layers create anti-reflection properties, an additional layer provides UV protection, and the outermost layer is hydrophobic (water-repellent) and oleophobic (oil-repellent). This premium coating stack, often called HMC (Hard Multi-Coat) in the optical industry, delivers the best overall lens performance for daily wear.',
        'benefits': [
            'Multi-layer anti-reflection coating for superior clarity',
            'Hard coat layer resists scratches from daily handling',
            'UV 400 protection blocks 100% of ultraviolet rays',
            'Hydrophobic top coat repels water, oil, and fingerprints',
            'Easier to clean \u2014 smudges wipe away effortlessly',
            'Improved durability compared to single-layer coatings'
        ],
        'who_should_use': 'Multi anti-glare lenses are ideal for anyone who wants the most complete lens protection in a single package. They are especially suited for professionals who need pristine lens clarity, people who work in varying environments (indoor/outdoor), and anyone who finds themselves frequently cleaning their glasses. If you want the best all-round lens coating without choosing individual add-ons, multi anti-glare is the premium choice.',
        'specs': [
            {'label': 'Coating Layers', 'value': '7-Layer Stack'},
            {'label': 'UV Protection', 'value': 'UV400 (100%)'},
            {'label': 'Water Repellent', 'value': 'Hydrophobic'},
            {'label': 'Add-on Price', 'value': 'Rs 200'}
        ],
        'how_it_works': 'Choose any spectacle frame from Optiwar and select Multi Anti-Glare during the prescription step. At Rs 200, this premium coating upgrade is applied to both lenses. It includes all the benefits of anti-glare plus scratch resistance, UV blocking, and water repellency. Base lenses are always complimentary with every frame purchase.',
        'faqs': [
            {'q': 'What is the difference between anti-glare and multi anti-glare?', 'a': 'Standard anti-glare provides reflection reduction only. Multi anti-glare adds scratch resistance, UV protection, hydrophobic coating, and oleophobic coating on top of the anti-reflection layers. It is a comprehensive all-in-one coating.'},
            {'q': 'Is multi anti-glare worth the extra cost?', 'a': 'At Rs 200 (just Rs 150 more than basic anti-glare), you get scratch protection, UV blocking, and easy-clean hydrophobic coating. For most users, the added durability and convenience make it an excellent value.'},
            {'q': 'How do I clean multi anti-glare lenses?', 'a': 'Use the provided microfiber cloth with gentle circular motions. The hydrophobic coating means most smudges wipe off easily. Avoid paper tissues, rough fabrics, or household glass cleaners.'}
        ]
    },
    'thin-lenses': {
        'name': 'Thin Lenses',
        'name_short': 'Thin Lenses',
        'slug': 'thin-lenses',
        'price': 100,
        'price_label': 'Add-on Rs 100 with any frame',
        'color': '#e0f7fa',
        'icon': '\U0001f4a0',
        'widget_class': 'thin',
        'widget_icon': '\u25cc',
        'widget_label': 'Thin Lenses',
        'short_desc': 'Slimmer, lighter lenses for a better look and more comfortable fit. Ideal for moderate prescriptions.',
        'meta_description': 'Thin Lenses (1.56 index) at Rs 100 add-on at Optiwar. Lighter and thinner than standard lenses for moderate prescriptions. Better aesthetics and comfort. Free shipping India.',
        'keywords': 'thin lenses, 1.56 index lenses, thinner spectacle lenses, lightweight eyeglass lenses India, optiwar thin lenses',
        'what_are': 'Thin lenses use a higher refractive index material (1.56) compared to standard CR-39 plastic (1.49). The higher refractive index means light bends more efficiently through the material, allowing the lens to be made thinner while providing the same optical correction. For prescriptions between -2.00 and -4.00 (or equivalent plus powers), standard lenses can appear noticeably thick at the edges. Thin lenses solve this by reducing edge thickness by approximately 20-30%, resulting in a sleeker profile that looks better in all frame styles.',
        'benefits': [
            '20-30% thinner than standard CR-39 plastic lenses',
            'Noticeably lighter weight for improved all-day comfort',
            'Better cosmetic appearance \u2014 no thick edges showing through frames',
            'Compatible with all frame styles including rimless and semi-rimless',
            'Improved aesthetics for moderate to moderately-high prescriptions',
            'Same optical clarity as standard lenses'
        ],
        'who_should_use': 'Thin lenses are recommended for anyone with a prescription power between -2.00 and +2.00 who wants a slimmer, more attractive lens profile. They are especially beneficial for people who wear rimless or semi-rimless frames where lens edge thickness is visible. If your current glasses feel heavy on your nose or you are self-conscious about thick lens edges, thin lenses provide an affordable upgrade.',
        'specs': [
            {'label': 'Refractive Index', 'value': '1.56'},
            {'label': 'Material', 'value': 'Mid-Index Plastic'},
            {'label': 'Weight Reduction', 'value': '~20%'},
            {'label': 'Add-on Price', 'value': 'Rs 100'}
        ],
        'how_it_works': 'Select any Optiwar spectacle frame and choose Thin Lenses during the prescription step. The add-on costs Rs 100. For prescriptions up to \u00b18.00, our system automatically configures the right material. If your power exceeds \u00b18.00, a high-power surcharge applies. Base single-vision lenses are always complimentary with your frame.',
        'faqs': [
            {'q': 'How much thinner are thin lenses compared to regular?', 'a': 'For a typical -3.00 prescription, thin lenses (1.56 index) are approximately 20-30% thinner at the edges compared to standard 1.49 index lenses. The visual difference is noticeable, especially in smaller frames.'},
            {'q': 'Should I choose thin or ultra-thin lenses?', 'a': 'For prescriptions between -2.00 and -4.00, thin lenses (Rs 100) provide a good balance of thinness and value. For prescriptions above -4.00, ultra-thin lenses (Rs 350, 1.61 index) are recommended for the best cosmetic result.'},
            {'q': 'Do thin lenses affect optical quality?', 'a': 'No. Thin lenses provide the same optical clarity and accuracy as standard lenses. The higher index material simply allows the same correction in a thinner profile.'}
        ]
    },
    'ultra-thin-lenses': {
        'name': 'Ultra-Thin Lenses',
        'name_short': 'Ultra-Thin Lenses',
        'slug': 'ultra-thin-lenses',
        'price': 350,
        'price_label': 'Add-on Rs 350 with any frame',
        'color': '#ede7f6',
        'icon': '\u2728',
        'widget_class': 'ultra-thin',
        'widget_icon': '\u25ce',
        'widget_label': 'Ultra Thin',
        'short_desc': 'The thinnest, lightest lenses available. High-index 1.61 material for high prescriptions without the bulk.',
        'meta_description': 'Ultra-Thin Lenses (1.61 high-index) at Rs 350 add-on at Optiwar. The thinnest and lightest prescription lenses for high powers. Up to 40% thinner than standard. Free shipping India.',
        'keywords': 'ultra-thin lenses, 1.61 high index lenses, extra thin spectacle lenses, high power thin lenses India, optiwar ultra-thin',
        'what_are': 'Ultra-thin lenses use premium 1.61 high-index material, the thinnest lens option available for single-vision prescriptions. This advanced material bends light more efficiently than any standard plastic, allowing lenses to be crafted up to 40% thinner than conventional CR-39. For people with higher prescriptions (-4.00 and above), standard lenses can become unattractively thick and heavy. Ultra-thin lenses transform a high prescription into a sleek, lightweight lens that looks and feels like a much lower power.',
        'benefits': [
            'Up to 40% thinner than standard CR-39 lenses',
            'Significantly lighter \u2014 reduces nose bridge pressure',
            'Minimal edge thickness even at high prescriptions',
            'Looks great in any frame style, including rimless',
            'Built-in UV protection',
            'High-index material provides excellent optical clarity'
        ],
        'who_should_use': 'Ultra-thin lenses are strongly recommended for anyone with a prescription power of -4.00 or higher (or equivalent plus power). They are essential for rimless and semi-rimless frame wearers with moderate to high prescriptions. If heavy, thick glasses have been a concern for you, ultra-thin lenses will make a dramatic difference in both appearance and comfort.',
        'specs': [
            {'label': 'Refractive Index', 'value': '1.61'},
            {'label': 'Material', 'value': 'High-Index Plastic'},
            {'label': 'Thickness Reduction', 'value': 'Up to 40%'},
            {'label': 'Add-on Price', 'value': 'Rs 350'}
        ],
        'how_it_works': 'Choose any spectacle frame from Optiwar and select Ultra-Thin Lenses during the prescription step. The Rs 350 add-on applies to both lenses. Ultra-thin material is especially beneficial for prescriptions above -4.00. Our AI system checks your entered power and recommends the optimal material automatically. Free shipping across India.',
        'faqs': [
            {'q': 'What prescription power needs ultra-thin lenses?', 'a': 'We recommend ultra-thin (1.61 index) for prescriptions of -4.00 and above. Below -4.00, thin lenses (1.56 index at Rs 100) are usually sufficient. Below -2.00, standard lenses work fine.'},
            {'q': 'Are ultra-thin lenses fragile?', 'a': 'No. 1.61 high-index lenses are durable for everyday use. However, like all high-index materials, they benefit from a scratch-resistant coating. Combining ultra-thin with multi anti-glare coating gives the best protection.'},
            {'q': 'Can ultra-thin lenses be tinted?', 'a': 'Yes. Ultra-thin lenses can accept tints, photochromic treatments, and all coating options. Discuss any combination requirements during checkout.'}
        ]
    },
    'photogrey-lenses': {
        'name': 'Photogrey Lenses (Photochromic Grey)',
        'name_short': 'Photogrey Lenses',
        'slug': 'photogrey-lenses',
        'price': 350,
        'price_label': 'Add-on Rs 350 with any frame',
        'color': '#eceff1',
        'icon': '\U0001f576\ufe0f',
        'widget_class': 'grey',
        'widget_icon': '\U0001f311',
        'widget_label': 'Photo Grey',
        'short_desc': 'Lenses that automatically darken in sunlight and clear indoors. Grey tint for true color perception outdoors.',
        'meta_description': 'Photogrey (Photochromic Grey) Lenses at Rs 350 add-on at Optiwar. Auto-darkening lenses that turn grey in sunlight and clear indoors. No need for separate sunglasses. Free shipping India.',
        'keywords': 'photogrey lenses, photochromic grey lenses, auto tint lenses India, transition lenses grey, light reactive lenses, optiwar photogrey',
        'what_are': 'Photogrey lenses are photochromic lenses that contain special molecules which react to ultraviolet (UV) light. When exposed to sunlight, these molecules change structure and darken the lens to a neutral grey tint, acting like sunglasses. When you move indoors or away from UV light, the molecules reverse and the lenses become clear again. The grey tint provides true color perception \u2014 colors appear natural without the warm shift that brown tints produce. This makes photogrey lenses the most popular choice for people who want one pair of glasses for both indoor and outdoor use.',
        'benefits': [
            'Automatically darkens to grey in sunlight (UV-activated)',
            'Returns to fully clear indoors within minutes',
            'True color perception \u2014 grey tint does not distort colors',
            'Eliminates the need to carry separate sunglasses',
            'Provides 100% UV protection when activated',
            'Comfortable in varying light conditions throughout the day'
        ],
        'who_should_use': 'Photogrey lenses are ideal for anyone who frequently moves between indoors and outdoors \u2014 commuters, delivery personnel, teachers, sales professionals, and outdoor enthusiasts. They are especially convenient for people who dislike carrying or switching between prescription glasses and sunglasses. If you prefer a neutral, natural-looking tint that does not alter color perception, photogrey is the right choice over photobrown.',
        'specs': [
            {'label': 'Tint Color', 'value': 'Neutral Grey'},
            {'label': 'Activation', 'value': 'UV Light'},
            {'label': 'Clear-to-Dark Time', 'value': '~30 seconds'},
            {'label': 'Add-on Price', 'value': 'Rs 350'}
        ],
        'how_it_works': 'Select any Optiwar spectacle frame and choose Photogrey during the prescription step. At Rs 350, the photochromic grey treatment is applied to both lenses. Indoors, your lenses stay clear. Step outside and they darken within about 30 seconds. They return to clear within 2-3 minutes when you go back inside. Our system configures the lens material based on your prescription automatically.',
        'faqs': [
            {'q': 'Do photogrey lenses work inside a car?', 'a': 'Standard photochromic lenses respond to UV light. Since car windshields block most UV, photogrey lenses may not darken fully inside a car. For driving, polarized lenses or dedicated driving sunglasses are recommended.'},
            {'q': 'How long do photogrey lenses last?', 'a': 'The photochromic molecules remain active for 2-3 years with normal use. Over time, the darkening response may gradually reduce. Lens replacement is recommended after 2-3 years for optimal performance.'},
            {'q': 'What is the difference between photogrey and photobrown?', 'a': 'Photogrey darkens to a neutral grey tint for true color perception. Photobrown darkens to a warm brown/amber tint that enhances contrast. Grey is preferred for general use; brown is popular for driving and sports.'}
        ]
    },
    'photobrown-lenses': {
        'name': 'Photobrown Lenses (Photochromic Brown)',
        'name_short': 'Photobrown Lenses',
        'slug': 'photobrown-lenses',
        'price': 650,
        'price_label': 'Add-on Rs 650 with any frame',
        'color': '#efebe9',
        'icon': '\U0001f7e4',
        'widget_class': 'brown',
        'widget_icon': '\U0001f7e4',
        'widget_label': 'Photo Brown',
        'short_desc': 'Premium photochromic lenses with warm brown tint. Enhanced contrast for driving and outdoor activities.',
        'meta_description': 'Photobrown (Photochromic Brown) Lenses at Rs 650 add-on at Optiwar. Auto-darkening lenses with warm brown tint for enhanced contrast. Ideal for driving and sports. Free shipping India.',
        'keywords': 'photobrown lenses, photochromic brown lenses, brown transition lenses India, contrast enhancing lenses, driving photochromic lenses, optiwar photobrown',
        'what_are': "Photobrown lenses are premium photochromic lenses that darken to a warm brown/amber tint when exposed to UV light. Unlike the neutral grey of photogrey lenses, the brown tint actively enhances contrast and depth perception, making objects appear sharper against backgrounds. The amber-brown color filters blue light naturally and increases visual definition, which is why brown-tinted lenses are favored by drivers, golfers, cyclists, and anyone who performs activities requiring sharp distance vision. Indoors, the lenses return to clear, functioning as regular prescription glasses.",
        'benefits': [
            'Premium warm brown tint enhances contrast and depth perception',
            'Superior visual definition for driving and outdoor sports',
            'Auto-darkens in sunlight and clears fully indoors',
            'Natural blue-light filtering when activated',
            'Reduces eye fatigue in bright outdoor conditions',
            'Stylish warm tint complements most skin tones and frame colors'
        ],
        'who_should_use': 'Photobrown lenses are the premium choice for people who spend considerable time driving, playing sports, or engaging in outdoor activities where contrast and depth perception matter. Golfers, cyclists, runners, and frequent drivers particularly benefit from the contrast-enhancing brown tint. If you prefer a warmer-toned lens over neutral grey, photobrown provides both style and functional advantages.',
        'specs': [
            {'label': 'Tint Color', 'value': 'Warm Brown/Amber'},
            {'label': 'Contrast Enhancement', 'value': 'High'},
            {'label': 'Activation', 'value': 'UV Light'},
            {'label': 'Add-on Price', 'value': 'Rs 650'}
        ],
        'how_it_works': 'Choose any spectacle frame from Optiwar and select Photobrown during the prescription step. At Rs 650, this premium photochromic treatment is applied to both lenses. The brown-tinted photochromic compound is embedded in the lens material for consistent, long-lasting performance. Base prescription lenses are always complimentary. Free shipping across India.',
        'faqs': [
            {'q': 'Why are photobrown lenses more expensive than photogrey?', 'a': 'Photobrown lenses use a more advanced photochromic compound that provides contrast enhancement in addition to light adaptation. The brown-amber formulation requires a more complex manufacturing process, resulting in the higher price point.'},
            {'q': 'Are photobrown lenses good for driving?', 'a': 'Yes. The brown tint enhances contrast and reduces glare from road surfaces, making them excellent for daytime driving. However, like all photochromic lenses, they may not darken fully behind UV-blocking windshields.'},
            {'q': 'Can I combine photobrown with anti-glare?', 'a': 'Yes. Adding multi anti-glare coating (Rs 200) to photobrown lenses provides the best combination of light adaptation, contrast enhancement, and reflection reduction.'}
        ]
    },
    'polarized-lenses': {
        'name': 'Polarized Lenses',
        'name_short': 'Polarized Lenses',
        'slug': 'polarized-lenses',
        'price': 800,
        'price_label': 'Premium Add-on Rs 800 with any frame',
        'color': '#e8eaf6',
        'icon': '\u2600\ufe0f',
        'widget_class': 'polarized',
        'widget_icon': '\U0001f576',
        'widget_label': 'Polarized',
        'short_desc': 'Eliminate horizontal glare from water, roads, and snow. The ultimate lens for driving, fishing, and outdoor sports.',
        'meta_description': 'Polarized Prescription Lenses at Rs 800 add-on at Optiwar. Eliminates glare from water, roads, and snow. Best for driving, fishing, and outdoor activities. Free shipping India.',
        'keywords': 'polarized lenses India, polarized prescription lenses, anti-glare driving lenses, polarized sunglasses lenses, fishing lenses polarized, optiwar polarized',
        'what_are': 'Polarized lenses contain a special filter that blocks horizontally-oriented light waves \u2014 the type of light that creates intense glare when sunlight reflects off flat surfaces like water, roads, car hoods, snow, and glass buildings. Unlike regular tinted lenses that simply reduce overall brightness, polarized lenses selectively eliminate glare while maintaining clear, sharp vision. This is achieved through a laminated filter with vertically-aligned molecules that act like microscopic venetian blinds, blocking horizontal light while allowing useful vertical light to pass through.',
        'benefits': [
            'Eliminates intense glare from water, roads, snow, and reflective surfaces',
            'Dramatically improves visual comfort in bright outdoor conditions',
            'Enhances color saturation and contrast \u2014 colors appear richer and more vivid',
            'Reduces eye strain and fatigue during outdoor activities',
            'Provides 100% UV protection',
            'Essential safety feature for driving \u2014 reduces dangerous road glare'
        ],
        'who_should_use': 'Polarized lenses are essential for drivers who face road glare, fishermen who need to see through water surface reflections, boaters, skiers, and anyone who spends significant time outdoors in bright conditions. They are also highly recommended for beach-goers, cyclists, and outdoor sports enthusiasts. If you find yourself squinting frequently in sunlight or are bothered by reflections from flat surfaces, polarized lenses will transform your visual experience.',
        'specs': [
            {'label': 'Filter Type', 'value': 'Horizontal Glare Block'},
            {'label': 'UV Protection', 'value': 'UV400 (100%)'},
            {'label': 'Best For', 'value': 'Driving, Water Sports'},
            {'label': 'Add-on Price', 'value': 'Rs 800'}
        ],
        'how_it_works': 'Select any spectacle frame from Optiwar and request Polarized Lenses during the prescription step. At Rs 800, the polarized filter is built into both prescription lenses. Available in grey and brown tint options. Contact our team during checkout to specify your preference. Base prescription lenses are complimentary with every frame. Free shipping across India.',
        'faqs': [
            {'q': 'Can I get polarized lenses with my prescription?', 'a': 'Yes. Optiwar offers polarized lenses in prescription form for single-vision corrections. Simply enter your prescription as usual during checkout and select the polarized add-on.'},
            {'q': 'Are polarized lenses good for everyday use?', 'a': 'While polarized lenses excel outdoors, they can make some LCD screens appear dark or show rainbow patterns at certain angles. For primarily indoor use, anti-glare or blue cut lenses are more suitable. Polarized lenses are best as a dedicated outdoor/driving pair.'},
            {'q': 'What is the difference between polarized and photochromic?', 'a': 'Photochromic lenses automatically darken/lighten based on UV exposure. Polarized lenses block horizontal glare regardless of brightness. They serve different purposes: photochromic adapts to light levels, polarized eliminates surface glare. Some premium lenses combine both technologies.'}
        ]
    },
    'bifocal-round-lenses': {
        'name': 'Bifocal Round Type KT Lenses',
        'name_short': 'Bifocal Round Lenses',
        'slug': 'bifocal-round-lenses',
        'price': 250,
        'price_label': 'Add-on Rs 250 with any frame',
        'color': '#fff3e0',
        'icon': '\U0001f441\ufe0f',
        'widget_class': 'bifocal-round',
        'widget_icon': '\u25d4',
        'widget_label': 'Bifocal KT',
        'short_desc': 'Classic round-segment bifocal (Kryptok style) for distance and near vision in one lens. Affordable multifocal solution.',
        'meta_description': 'Bifocal Round Type KT (Kryptok) Lenses at Rs 250 add-on at Optiwar. Classic round-segment bifocal for distance and reading in one lens. Ideal for presbyopia. Free shipping India.',
        'keywords': 'bifocal round lenses, kryptok bifocal lenses India, round segment bifocal, KT bifocal lenses, presbyopia lenses, reading glasses bifocal, optiwar bifocal',
        'what_are': "Bifocal Round Type KT (Kryptok) lenses are the classic bifocal design where a small round segment for near/reading vision is seamlessly fused into the lower portion of a distance vision lens. The KT (Kryptok) design features a circular near-vision area that blends smoothly into the main lens, making the bifocal line less visible compared to flat-top designs. This is the most time-tested bifocal format, used for over a century to help people with presbyopia (age-related difficulty in reading) see clearly at both distance and near without switching between two pairs of glasses.",
        'benefits': [
            'See clearly at both distance and near with one pair of glasses',
            'Round segment blends more naturally into the lens than flat-top designs',
            'Proven, time-tested design trusted by optical professionals worldwide',
            'Most affordable bifocal option at just Rs 250',
            'No adaptation period needed for most users',
            'Eliminates the need to carry separate reading glasses'
        ],
        'who_should_use': "Bifocal Round KT lenses are designed for people with presbyopia \u2014 typically those aged 40 and above who need different prescriptions for distance and reading. If you find yourself holding your phone or book at arm's length to read, or if you currently switch between distance glasses and reading glasses, bifocal lenses will solve both needs in one pair. The round KT style is preferred by people who want a more discreet bifocal line.",
        'specs': [
            {'label': 'Segment Shape', 'value': 'Round (Kryptok)'},
            {'label': 'Near Zone', 'value': 'Circular Fused'},
            {'label': 'Best For', 'value': 'Presbyopia (40+)'},
            {'label': 'Add-on Price', 'value': 'Rs 250'}
        ],
        'how_it_works': 'When entering your prescription at Optiwar, click the ADD column to enter your Addition power (typically +1.00 to +3.00 as prescribed by your eye doctor). The system will automatically show bifocal options. Select Bifocal Round Type KT at Rs 250. You will need both your distance power (SPH, CYL, AXIS) and your Addition power. If unsure, consult your prescription or contact our support team.',
        'faqs': [
            {'q': 'What is Addition power (ADD)?', 'a': 'Addition power is the extra magnification added to the lower portion of a bifocal lens for near/reading vision. It is prescribed by your eye doctor and typically ranges from +1.00 to +3.00 depending on your age and reading needs.'},
            {'q': 'What is the difference between round KT and flat-top D bifocals?', 'a': 'Round KT has a circular near-vision segment that blends more naturally into the lens. Flat-top D has a wider, D-shaped segment that provides a larger near-vision area. KT is more discreet; D-type offers a wider reading zone.'},
            {'q': 'Can I upgrade from single vision to bifocal later?', 'a': 'Bifocal lenses require a different lens design from the start. If you currently have single-vision glasses and your doctor prescribes an Addition power, you will need new bifocal lenses. At Optiwar, simply order a new frame (or request lens-only replacement) with your full bifocal prescription.'}
        ]
    },
    'bifocal-flat-top-d-lenses': {
        'name': 'Bifocal Flat-Top D Style Lenses',
        'name_short': 'Bifocal Flat-Top D Lenses',
        'slug': 'bifocal-flat-top-d-lenses',
        'price': 500,
        'price_label': 'Add-on Rs 500 with any frame',
        'color': '#fff8e1',
        'icon': '\U0001f453',
        'widget_class': 'bifocal-flat',
        'widget_icon': '\u25d3',
        'widget_label': 'Bifocal D',
        'short_desc': 'Wide-segment D-shaped bifocal for a larger reading area. The most popular bifocal design worldwide.',
        'meta_description': 'Bifocal Flat-Top D Style Lenses at Rs 500 add-on at Optiwar. Wide D-shaped reading segment for comfortable near vision. Most popular bifocal design. Free shipping India.',
        'keywords': 'bifocal flat top D lenses, D segment bifocal India, flat top bifocal lenses, wide reading segment bifocal, presbyopia lenses D type, optiwar bifocal D',
        'what_are': 'Bifocal Flat-Top D lenses are the most widely used bifocal design in the world. They feature a D-shaped (flat-top) reading segment in the lower half of the lens with a clearly defined straight line at the top of the near-vision zone. The flat-top design provides a wider reading area compared to round KT bifocals, making it easier to read books, newspapers, phone screens, and do close-up work. The straight line makes it easy to locate the reading zone and provides a consistent, predictable transition between distance and near vision.',
        'benefits': [
            'Wider reading segment than round bifocals \u2014 more comfortable for prolonged reading',
            'Clear, defined transition line makes it easy to find the reading zone',
            'Most prescribed bifocal design worldwide \u2014 trusted by opticians',
            'Excellent for extended near-work like reading, sewing, or phone use',
            'Available for all prescription powers and Addition values',
            'Better peripheral near vision compared to round segment'
        ],
        'who_should_use': 'Bifocal Flat-Top D lenses are recommended for people with presbyopia who do significant amounts of reading or close-up work. They are the default choice for most bifocal wearers because the wider D-segment offers more usable reading area. If your eye doctor prescribes bifocals and you read frequently, work with your hands at close range, or use your phone extensively, the Flat-Top D provides the most comfortable near-vision experience.',
        'specs': [
            {'label': 'Segment Shape', 'value': 'D-Shaped (Flat-Top)'},
            {'label': 'Reading Width', 'value': '28mm Standard'},
            {'label': 'Best For', 'value': 'Heavy Readers (40+)'},
            {'label': 'Add-on Price', 'value': 'Rs 500'}
        ],
        'how_it_works': 'Enter your full prescription including Addition power (ADD) during the Optiwar prescription step. When you enter ADD values, the system shows bifocal options. Select Bifocal Flat-Top Type D at Rs 500. This is auto-selected as the default bifocal option because of its popularity and comfort. Both lenses are crafted with the D-segment positioned for optimal reading comfort.',
        'faqs': [
            {'q': 'Why is flat-top D more expensive than round KT?', 'a': 'The Flat-Top D segment requires more precise manufacturing to create the wider, accurately-positioned D-shaped reading zone. The larger segment also uses more material. The additional cost reflects better near-vision comfort.'},
            {'q': 'Is there a visible line on the lens?', 'a': 'Yes. All bifocal lenses have a visible line where the near segment begins. This is normal and helps you quickly find your reading zone. If you prefer no visible line, consider Progressive Lenses (Rs 1000) which offer smooth, line-free transitions.'},
            {'q': 'Can I use flat-top D bifocals for computer work?', 'a': 'Bifocals provide distance and near vision but have limited intermediate (computer distance) correction. For heavy computer users, progressive lenses may be more suitable as they include an intermediate zone. Discuss your needs with your eye doctor.'}
        ]
    },
    'progressive-lenses': {
        'name': 'Progressive Lenses',
        'name_short': 'Progressive Lenses',
        'slug': 'progressive-lenses',
        'price': 1000,
        'price_label': 'Add-on Rs 1000 with any frame',
        'color': '#f3e5f5',
        'icon': '\U0001f3af',
        'widget_class': 'progressive',
        'widget_icon': '\u2b12',
        'widget_label': 'Progressive',
        'short_desc': 'Line-free multifocal lenses with smooth transition from distance to near. The modern alternative to bifocals.',
        'meta_description': 'Progressive Lenses at Rs 1000 add-on at Optiwar. Line-free multifocal with smooth distance-intermediate-near transition. Modern alternative to bifocals for presbyopia. Free shipping India.',
        'keywords': 'progressive lenses India, varifocal lenses, no-line bifocal lenses, multifocal lenses India, progressive addition lenses, PAL lenses, optiwar progressive',
        'what_are': 'Progressive lenses, also called varifocal or PAL (Progressive Addition Lenses), are the most advanced multifocal lens design available. Unlike bifocals which have a visible line separating distance and near zones, progressive lenses provide a seamless, gradual transition through all viewing distances \u2014 distance vision at the top, intermediate (computer distance) in the middle, and near/reading vision at the bottom. There is no visible line on the lens, making them look exactly like single-vision glasses. This design provides natural, comfortable vision at every distance, mimicking how the eye naturally focuses at different ranges.',
        'benefits': [
            'No visible line on the lens \u2014 looks identical to single-vision glasses',
            'Smooth, natural transition through all distances (far, intermediate, near)',
            'Includes intermediate zone for comfortable computer use',
            'Cosmetically superior to bifocals \u2014 no age-telling bifocal line',
            'More natural visual experience compared to the abrupt jump of bifocals',
            'Available for all prescription powers and Addition values'
        ],
        'who_should_use': 'Progressive lenses are ideal for people with presbyopia who want the most natural, seamless visual experience and prefer not to have a visible bifocal line on their glasses. They are especially recommended for professionals who work at multiple distances throughout the day \u2014 looking at a computer, reading documents, and seeing across the room. If appearance matters to you and you want one pair of glasses for all distances, progressives are the premium choice.',
        'specs': [
            {'label': 'Vision Zones', 'value': 'Distance + Mid + Near'},
            {'label': 'Visible Line', 'value': 'None (Seamless)'},
            {'label': 'Adaptation Period', 'value': '1-2 Weeks'},
            {'label': 'Add-on Price', 'value': 'Rs 1000'}
        ],
        'how_it_works': 'Enter your complete prescription including Addition power (ADD) at Optiwar checkout. When ADD values are entered, the system shows multifocal options. Select Progressive Lenses at Rs 1000. Both lenses are crafted with progressive corridors tailored to your prescription. A short adaptation period of 1-2 weeks is normal as your eyes learn to use different zones of the lens. Move your head (not just your eyes) to look through the correct zone for each distance.',
        'faqs': [
            {'q': 'Is there an adaptation period for progressive lenses?', 'a': 'Yes. Most people adapt within 1-2 weeks. During this period, you may notice slight peripheral blur or a swimming sensation when moving your head. This is normal. Wear your progressives consistently (not switching with old glasses) to adapt faster.'},
            {'q': 'Why are progressive lenses more expensive than bifocals?', 'a': 'Progressive lenses require significantly more complex manufacturing. Each lens has a precisely calculated corridor of power change that must be custom-generated. The smooth gradient design requires advanced free-form or mold technology, resulting in higher costs.'},
            {'q': 'Can anyone wear progressive lenses?', 'a': 'Most people with presbyopia can successfully wear progressive lenses. However, people with very high cylinder (astigmatism) or those who have worn bifocals for many years may find adaptation more challenging. Start with progressives as early as possible for the easiest transition.'}
        ]
    }
}


def _localize_lens_for_global(lens, eur_map):
    """Deep-copy a LENS_DATA entry with INR/India copy converted to EUR/worldwide
    for the global site (optiwar.com). The India site is never passed here, so
    its copy is unaffected. Walks every customer-facing text field so no 'Rs' or
    India-shipping phrasing can leak onto .com."""
    import copy as _copy
    d = _copy.deepcopy(lens)
    eur_price = eur_map.get(d['price'], str(d['price']))
    d['price_eur'] = eur_price

    def conv(s):
        if not isinstance(s, str):
            return s
        # currency: swap every known INR add-on tier for its EUR equivalent
        # (longest INR number first so e.g. 'Rs 100' can't clobber 'Rs 1000')
        for inr in sorted(eur_map, key=lambda x: -len(str(x))):
            s = s.replace('Rs ' + str(inr), '€' + str(eur_map[inr]))
        # prose price-difference that isn't an add-on tier
        s = s.replace('Rs 150 more', '€2 more')
        # shipping / India framing
        s = (s.replace('Free shipping across India', 'Free worldwide shipping')
               .replace('Free shipping India.', 'Free worldwide shipping.')
               .replace('Free shipping India', 'Free worldwide shipping')
               .replace('shipping across India', 'worldwide shipping')
               .replace('across India', 'worldwide'))
        return s

    for f in ('price_label', 'meta_description', 'how_it_works',
              'short_desc', 'what_are', 'who_should_use'):
        if isinstance(d.get(f), str):
            d[f] = conv(d[f])
    if isinstance(d.get('benefits'), list):
        d['benefits'] = [conv(x) for x in d['benefits']]
    for spec in d.get('specs', []):
        if spec.get('label') == 'Add-on Price':
            spec['value'] = '€' + eur_price
        elif isinstance(spec.get('value'), str):
            spec['value'] = conv(spec['value'])
    for faq in d.get('faqs', []):
        if isinstance(faq.get('q'), str):
            faq['q'] = conv(faq['q'])
        if isinstance(faq.get('a'), str):
            faq['a'] = conv(faq['a'])
    if isinstance(d.get('keywords'), str):
        d['keywords'] = conv(d['keywords']).replace('India', 'online').replace('india', 'online')
    return d


@bp.route('/lenses')
def lenses_hub():
    """Hub page listing all lens types available at Optiwar."""
    # Read EUR prices from lens_pricing.json config
    import json as _json
    _eur_map = {}
    try:
        with open("/var/www/flask-optiwar-ow-release-090525/lens_pricing.json", "r") as _f:
            _pc = _json.load(_f)
        for _cat in _pc.get("default", {}).values():
            for _code, _item in _cat.items():
                _eur_map[_item["price"]] = str(_item.get("price_eur", round((_item["price"] + 3000) / 100, 2)))
    except Exception:
        pass
    EUR_LENS_PRICES = _eur_map if _eur_map else {50: '5', 100: '7', 200: '7', 250: '5.50', 350: '6.50', 500: '8', 650: '9.50', 800: '11', 1000: '13'}
    is_india_site = _req_is_india()
    lenses_list = []
    for v in LENS_DATA.values():
        src = v if is_india_site else _localize_lens_for_global(v, EUR_LENS_PRICES)
        lenses_list.append({
            'name': src['name'],
            'slug': src['slug'],
            'price_label': src['price_label'],
            'short_desc': src['short_desc'],
            'color': src['color'],
            'icon': src['icon'],
            'widget_class': src['widget_class'],
            'widget_icon': src['widget_icon'],
            'widget_label': src['widget_label']
        })
    return render_template('lenses.html', lenses=lenses_list)


@bp.route('/lenses/<lens_slug>')
def lens_page(lens_slug):
    """Individual lens type content page with full SEO/AEO/JSON-LD."""
    lens = LENS_DATA.get(lens_slug)
    if not lens:
        return "Lens type not found", 404
    # Read EUR prices from lens_pricing.json config
    import json as _json2
    _eur_map2 = {}
    try:
        with open("/var/www/flask-optiwar-ow-release-090525/lens_pricing.json", "r") as _f2:
            _pc2 = _json2.load(_f2)
        for _cat2 in _pc2.get("default", {}).values():
            for _code2, _item2 in _cat2.items():
                _eur_map2[_item2["price"]] = str(_item2.get("price_eur", round((_item2["price"] + 3000) / 100, 2)))
    except Exception:
        pass
    EUR_LENS_PRICES = _eur_map2 if _eur_map2 else {50: '5', 100: '7', 200: '7', 250: '5.50', 350: '6.50', 500: '8', 650: '9.50', 800: '11', 1000: '13'}
    is_india_site = _req_is_india()
    if is_india_site:
        import copy
        lens_data = copy.deepcopy(lens)
        lens_data['price_eur'] = EUR_LENS_PRICES.get(lens_data['price'], str(lens_data['price']))
    else:
        # optiwar.com: convert all INR/India copy to EUR/worldwide
        lens_data = _localize_lens_for_global(lens, EUR_LENS_PRICES)
    all_lenses = [{'name': v['name'], 'slug': v['slug'], 'widget_class': v['widget_class'], 'widget_icon': v['widget_icon']} for v in LENS_DATA.values()]
    return render_template('lens_type.html', lens=lens_data, all_lenses=all_lenses)


@bp.route('/sitemap_index.xml')
def sitemap_index_xml():
    """Sitemap index file pointing to individual sitemaps."""
    from datetime import datetime
    _sirh = request.host.lower()
    if not _req_is_india():
        _si_host = 'optiwar.com'
    elif 'optiwar.in' in _sirh:
        _si_host = 'optiwar.in'
    else:
        _si_host = 'in.optiwar.com'
    _today = datetime.utcnow().strftime('%Y-%m-%d')
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += f'  <sitemap><loc>https://{_si_host}/sitemap.xml</loc><lastmod>{_today}</lastmod></sitemap>\n'
    xml += f'  <sitemap><loc>https://{_si_host}/image-sitemap.xml</loc><lastmod>{_today}</lastmod></sitemap>\n'
    xml += '</sitemapindex>'
    response = make_response(xml)
    response.headers['Content-Type'] = 'application/xml'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@bp.route('/sitemap.xml')
def sitemap_xml():
    """Serve pre-generated static sitemap.xml (zero DB queries)."""
    import os
    static_sitemap = os.path.join(current_app.root_path, 'static', 'seo', 'sitemap.xml')
    if os.path.exists(static_sitemap):
        with open(static_sitemap, 'r') as f:
            xml = f.read()
    else:
        # Fallback: minimal sitemap
        xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        xml += f'  <url><loc>https://optiwar.com/</loc></url>\n'
        xml += '</urlset>'
    if _req_is_india():
        xml = xml.replace('https://in.optiwar.com', 'https://optiwar.in').replace('https://optiwar.com', 'https://optiwar.in')
        # The file is generated once for both storefronts, so the vertical
        # boundary is applied on the way out: a lens URL regenerated into it
        # must not become an indexable .in URL by a host string replacement.
        xml = strip_ineligible_urls(xml, SITE_IN)
    else:
        # Lens URLs are appended at request time rather than written into the
        # static file: the set is whatever the release flag currently returns,
        # and a regenerated file would otherwise decide it hours earlier.
        db = get_db()
        rows = _live_lens_rows(db.cursor())
        blocks = lens_seo.sitemap_urls(rows, 'https://optiwar.com')
        if blocks and '</urlset>' in xml:
            xml = xml.replace('</urlset>',
                              '\n'.join(blocks) + '\n</urlset>')
    response = make_response(xml)
    response.headers['Content-Type'] = 'application/xml'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response

@bp.route('/image-sitemap.xml')
def image_sitemap_xml():
    """Google image sitemap: each product URL with its versioned master images
    (JPEG only, deduped). Storefront-aware host/currency-neutral URLs."""
    from xml.sax.saxutils import escape as _xesc
    _rh = request.host.lower()
    if not _req_is_india():
        host = 'optiwar.com'
    elif 'optiwar.in' in _rh:
        host = 'optiwar.in'
    else:
        host = 'in.optiwar.com'
    base = 'https://' + host

    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT product_id, product_category, product_slug, product_image,
               product_vertical
        FROM products
        WHERE (discontinued = 0 OR discontinued IS NULL)
          AND product_image IS NOT NULL AND product_image != ''
          AND product_slug IS NOT NULL AND product_slug != ''
          """ + catalogue_site_filter() + """
        ORDER BY product_id
    """)
    rows = cur.fetchall()

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
             'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">']
    parts += lens_seo.image_sitemap_urls(_live_lens_rows(cur), base)
    for p in rows:
        # Lenses are published above, from the release gate. Storefront
        # eligibility alone would advertise one the gate holds back, and
        # advertise a released one twice.
        if is_contact_lens(p):
            continue
        angles = versioned_angle_urls(p.get('product_image') or '', base, limit=25)
        if not angles:
            continue
        cat = (p.get('product_category') or '').lower().replace(' ', '-')
        loc = '%s/categories/%s/%s?pid=%s' % (base, cat, p['product_slug'], p['product_id'])
        parts.append('  <url>')
        parts.append('    <loc>%s</loc>' % _xesc(loc))
        for a in angles:
            parts.append('    <image:image><image:loc>%s</image:loc></image:image>' % _xesc(a))
        parts.append('  </url>')
    parts.append('</urlset>')

    response = make_response('\n'.join(parts))
    response.headers['Content-Type'] = 'application/xml'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@bp.route('/api/products', methods=['GET'])
def api_products():
    """
    Machine-readable JSON API for AI agents and programmatic access.
    Supports filtering by: category, color, size, min_price, max_price, shape, in_stock, q (search)
    Returns: JSON array of product objects with full details.
    """
    db = get_db()
    cursor = db.cursor()
    
    # Get query parameters
    category = request.args.get('category')
    color = request.args.get('color')
    size = request.args.get('size')
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    shape = request.args.get('shape')
    in_stock = request.args.get('in_stock', 'true')
    search_q = request.args.get('q')
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    per_page = min(per_page, 200)  # Max 200 per page
    # --- Redis cache (Phase B) ---
    _rc2 = get_redis()
    _cache_key2 = None
    if _rc2:
        _cache_key2 = f"products:{category}:{color}:{size}:{min_price}:{max_price}:{shape}:{in_stock}:{search_q}:{page}:{per_page}"
        _cached2 = _rc2.get(_cache_key2)
        if _cached2:
            resp2 = make_response(_cached2)
            resp2.headers['Content-Type'] = 'application/json'
            resp2.headers['X-Cache'] = 'HIT'
            return resp2
    # --- end cache check ---
    
    # Build query
    conditions = []
    params = []
    
    if category:
        conditions.append("product_category = %s")
        params.append(category)
    if color:
        conditions.append("LOWER(color_filter) = LOWER(%s)")
        params.append(color.strip())
    if size:
        conditions.append("product_perception_value LIKE %s")
        params.append(f'%{size}%')
    if min_price:
        conditions.append("product_special_price >= %s")
        params.append(min_price)
    if max_price:
        conditions.append("product_special_price <= %s")
        params.append(max_price)
    if in_stock.lower() in ('true', '1', 'yes'):
        conditions.append("product_quantity > 0")
    if search_q:
        conditions.append("(product_name LIKE %s OR product_code LIKE %s OR product_color LIKE %s)")
        params.extend([f'%{search_q}%', f'%{search_q}%', f'%{search_q}%'])
    if shape:
        shape_col = f'product_category_{shape.lower()}'
        # Validate shape column exists
        valid_shapes = ['rectangle', 'oval', 'round', 'square', 'wayfarer', 'horn',
                       'browline', 'aviator', 'cateye', 'clubmaster', 'panto', 'quatra', 'supra']
        if shape.lower() in valid_shapes:
            conditions.append(f"`{shape_col}` = 1")
    
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    offset = (page - 1) * per_page
    
    # Count total
    cursor.execute(f"SELECT COUNT(*) as cnt FROM products WHERE {where_clause}"
                   + catalogue_site_filter(), params)
    total_row = cursor.fetchone()
    total = total_row['cnt'] if isinstance(total_row, dict) else total_row[0]
    
    # Fetch products
    cursor.execute(f"""
        SELECT product_id, product_code, product_name, product_category,
               product_price, product_special_price, product_color,
               product_primary_color, product_secondary_color,
               product_size, product_perception_value, product_slug,
               product_quantity, product_image,
               product_category_rectangle, product_category_oval,
               product_category_round, product_category_square,
               product_category_wayfarer, product_category_aviator,
               product_category_cateye, product_category_clubmaster,
               product_category_panto, product_category_horn,
               product_category_browline
        FROM products 
        WHERE {where_clause}
        {catalogue_site_filter()}
        ORDER BY product_id DESC
        LIMIT %s OFFSET %s
    """, params + [per_page, offset])
    
    products = cursor.fetchall()
    cursor.close()
    
    # Format response
    result = []
    for p in products:
        if isinstance(p, dict):
            prod = p
        else:
            # Convert tuple to dict
            cols = ['product_id', 'product_code', 'product_name', 'product_category',
                    'product_price', 'product_special_price', 'product_color',
                    'product_primary_color', 'product_secondary_color',
                    'product_size', 'product_perception_value', 'product_slug',
                    'product_quantity', 'product_image',
                    'product_category_rectangle', 'product_category_oval',
                    'product_category_round', 'product_category_square',
                    'product_category_wayfarer', 'product_category_aviator',
                    'product_category_cateye', 'product_category_clubmaster',
                    'product_category_panto', 'product_category_horn',
                    'product_category_browline']
            prod = dict(zip(cols, p))
        
        # Determine shapes
        shapes = []
        shape_fields = {
            'rectangle': 'product_category_rectangle',
            'oval': 'product_category_oval',
            'round': 'product_category_round',
            'square': 'product_category_square',
            'wayfarer': 'product_category_wayfarer',
            'aviator': 'product_category_aviator',
            'cateye': 'product_category_cateye',
            'clubmaster': 'product_category_clubmaster',
            'panto': 'product_category_panto',
            'horn': 'product_category_horn',
            'browline': 'product_category_browline',
        }
        for shape_name, field in shape_fields.items():
            if prod.get(field) and int(prod[field]) == 1:
                shapes.append(shape_name)
        
        # Auto-generate description
        desc_parts = [prod.get('product_name', ''), prod.get('product_code', '')]
        if prod.get('product_color'):
            desc_parts.append(f"- {prod['product_color']}")
        desc_parts.append(prod.get('product_category', ''))
        if shapes:
            desc_parts.append(f"({', '.join(shapes)} shape)")
        if prod.get('product_size'):
            desc_parts.append(f"Size: {prod['product_size']}mm")
        if prod.get('product_perception_value'):
            desc_parts.append(f"Face fit: {prod['product_perception_value']}")
        
        description = ' '.join([p for p in desc_parts if p])
        
        # Build product URL
        slug = prod.get('product_slug', '')
        cat = prod.get('product_category', '')
        pid = prod.get('product_id', '')
        product_url = f"https://optiwar.com/categories/{cat}/{slug}?pid={pid}"
        
        # Build image URL
        image_url = None
        if prod.get('product_image'):
            first_img = prod['product_image'].split(',')[0].strip()
            image_url = f"https://optiwar.com/static/{first_img}"
        
        item = {
            "id": prod.get('product_id'),
            "code": prod.get('product_code'),
            "name": prod.get('product_name'),
            "category": prod.get('product_category'),
            "description": description,
            "price": {
                "mrp": float(prod.get('product_price', 0) or 0),
                "selling_price": float(prod.get('product_special_price', 0) or 0),
                "currency": "INR"
            },
            "color": {
                "display": prod.get('product_color'),
                "primary": prod.get('product_primary_color'),
                "secondary": prod.get('product_secondary_color')
            },
            "size": {
                "dimensions_mm": prod.get('product_size'),
                "face_fit": prod.get('product_perception_value')
            },
            "shapes": shapes,
            "in_stock": bool(prod.get('product_quantity') and int(prod.get('product_quantity', 0)) > 0),
            "stock_quantity": int(prod.get('product_quantity', 0) or 0),
            "url": product_url,
            "image": image_url,
            "schema_type": "Product"
        }
        result.append(item)
    
    response_data = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": "Optiwar Product Catalog",
        "description": "Complete product catalog from Optiwar factory outlet eyewear store",
        "numberOfItems": total,
        "pagination": {
            "page": page,
            "per_page": per_page,
            "total_pages": (total + per_page - 1) // per_page,
            "total_items": total
        },
        "filters_available": {
            "category": ["Spectacles Frame", "Hearing Aids", "Contact Lenses"],
            "shape": ["rectangle", "oval", "round", "square", "wayfarer", "aviator", "cateye", "clubmaster", "panto", "horn", "browline"],
            "size": ["Small", "Medium", "Large", "Extra Large"],
            "price_range": {"min": 0, "max": 10000, "currency": "INR"},
            "in_stock": ["true", "false"]
        },
        "items": result
    }
    
    response = make_response(jsonify(response_data))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['X-Robots-Tag'] = 'noindex'
    # --- Redis cache set (Phase B) ---
    if _rc2 and _cache_key2:
        try:
            _rc2.setex(_cache_key2, 300, response.get_data(as_text=True))
        except Exception as _ce2:
            print(f'[CACHE] products set error: {_ce2}')
    response.headers['X-Cache'] = 'MISS'
    return response


@bp.route('/api/products/<int:product_id>', methods=['GET'])
def api_product_detail(product_id):
    """Single product detail API for AI agents."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
    product = cursor.fetchone()
    cursor.close()
    
    if not product or not is_product_allowed(product):
        return jsonify({"error": "Product not found"}), 404
    
    if isinstance(product, dict):
        prod = product
    else:
        return jsonify({"error": "Internal format error"}), 500
    
    # Determine shapes
    shapes = []
    shape_fields = ['rectangle', 'oval', 'round', 'square', 'wayfarer', 'aviator',
                   'cateye', 'clubmaster', 'panto', 'horn', 'browline']
    for s in shape_fields:
        if prod.get(f'product_category_{s}') and int(prod[f'product_category_{s}']) == 1:
            shapes.append(s)
    
    # Auto-generate description
    desc_parts = [prod.get('product_name', ''), prod.get('product_code', '')]
    if prod.get('product_color'):
        desc_parts.append(f"in {prod['product_color']}")
    desc_parts.append(prod.get('product_category', ''))
    if shapes:
        desc_parts.append(f"with {', '.join(shapes)} shape")
    if prod.get('product_size'):
        desc_parts.append(f"- Frame dimensions: {prod['product_size']}mm (diameter-bridge-temple)")
    if prod.get('product_perception_value'):
        desc_parts.append(f"- Face fit: {prod['product_perception_value']}")
    description = ' '.join([p for p in desc_parts if p])
    
    # Build image URLs
    images = []
    if prod.get('product_image'):
        for img in prod['product_image'].split(','):
            images.append(f"https://optiwar.com/static/{img.strip()}")
    
    slug = prod.get('product_slug', '')
    cat = prod.get('product_category', '')
    product_url = f"https://optiwar.com/categories/{cat}/{slug}?pid={product_id}"
    
    result = {
        "@context": "https://schema.org",
        "@type": "Product",
        "productID": prod.get('product_id'),
        "sku": prod.get('product_code'),
        "name": f"{prod.get('product_name', '')} {prod.get('product_code', '')}",
        "brand": {"@type": "Brand", "name": prod.get('product_name', '')},
        "category": prod.get('product_category'),
        "description": description,
        "color": prod.get('product_color'),
        "size": prod.get('product_size'),
        "image": images,
        "url": product_url,
        "offers": {
            "@type": "Offer",
            "priceCurrency": "INR",
            "price": float(prod.get('product_special_price', 0) or 0),
            "priceValidUntil": "2027-12-31",
            "availability": "https://schema.org/InStock" if (prod.get('product_quantity') and int(prod.get('product_quantity', 0)) > 0) else "https://schema.org/OutOfStock"
        },
        "additionalProperty": [
            {"@type": "PropertyValue", "name": "Face Fit", "value": prod.get('product_perception_value')},
            {"@type": "PropertyValue", "name": "Frame Size (mm)", "value": prod.get('product_size')},
            {"@type": "PropertyValue", "name": "Primary Color", "value": prod.get('product_primary_color')},
            {"@type": "PropertyValue", "name": "Secondary Color", "value": prod.get('product_secondary_color')},
            {"@type": "PropertyValue", "name": "Shapes", "value": ', '.join(shapes)},
            {"@type": "PropertyValue", "name": "Stock Quantity", "value": int(prod.get('product_quantity', 0) or 0)},
            {"@type": "PropertyValue", "name": "MRP", "value": float(prod.get('product_price', 0) or 0)}
        ]
    }
    
    response = make_response(jsonify(result))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


@bp.route('/.well-known/apple-developer-merchantid-domain-association')
def apple_merchant_verification():
    """Serve Apple Pay merchant domain verification file for Razorpay."""
    verification_dir = os.path.join(os.path.dirname(__file__), 'static', '.well-known')
    return send_from_directory(verification_dir, 'apple-developer-merchantid-domain-association', mimetype='text/plain')

@bp.route('/.well-known/ai-plugin.json')
def ai_plugin_manifest():
    """OpenAI-style AI plugin manifest for discoverability by AI agents."""
    manifest = {
        "schema_version": "v1",
        "name_for_human": "Optiwar - Factory Outlet Eyewear",
        "name_for_model": "optiwar",
        "description_for_human": "Search and browse eyewear products at factory outlet prices from Optiwar India.",
        "description_for_model": "Use this to search, filter, and retrieve eyewear products (spectacle frames, hearing aids, contact lenses) from the Optiwar catalog. Products have attributes: name, code, color, shape, size (mm), price (INR), face fit, stock status. Filter by category, color, shape, price range, stock availability.",
        "auth": {"type": "none"},
        "api": {
            "type": "openapi",
            "url": "https://optiwar.com/api/openapi.json"
        },
        "logo_url": "https://optiwar.com/static/favicon-512x512.png",
        "contact_email": "support@optiwar.com",
        "legal_info_url": "https://optiwar.com/terms_and_conditions"
    }
    response = make_response(jsonify(manifest))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


@bp.route('/api/openapi.json')
def openapi_spec():
    """OpenAPI specification for AI agent integration."""
    spec = {
        "openapi": "3.0.0",
        "info": {
            "title": "Optiwar Product API",
            "description": "API to search and browse eyewear products from Optiwar factory outlet store. Supports INR and EUR currencies.",
            "version": "2.0.0",
            "contact": {"email": "support@optiwar.com"}
        },
        "servers": [{"url": "https://optiwar.com"}],
        "paths": {
            "/api/products": {
                "get": {
                    "summary": "Search and list products",
                    "description": "Get products with optional filters. Returns paginated results with full product details.",
                    "parameters": [
                        {"name": "category", "in": "query", "schema": {"type": "string", "enum": ["Spectacles Frame", "Hearing Aids", "Contact Lenses"]}},
                        {"name": "color", "in": "query", "schema": {"type": "string"}, "description": "Filter by color name (partial match)"},
                        {"name": "shape", "in": "query", "schema": {"type": "string", "enum": ["rectangle", "oval", "round", "square", "wayfarer", "aviator", "cateye", "clubmaster", "panto", "horn", "browline"]}},
                        {"name": "size", "in": "query", "schema": {"type": "string", "enum": ["Small", "Medium", "Large", "Extra Large"]}, "description": "Face fit / perception value"},
                        {"name": "min_price", "in": "query", "schema": {"type": "number"}, "description": "Minimum price in INR"},
                        {"name": "max_price", "in": "query", "schema": {"type": "number"}, "description": "Maximum price in INR"},
                        {"name": "in_stock", "in": "query", "schema": {"type": "string", "enum": ["true", "false"]}},
                        {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Free text search (name, code, color)"},
                        {"name": "page", "in": "query", "schema": {"type": "integer", "default": 1}},
                        {"name": "per_page", "in": "query", "schema": {"type": "integer", "default": 50, "maximum": 200}}
                    ],
                    "responses": {"200": {"description": "List of products"}}
                }
            },
            "/api/products/{product_id}": {
                "get": {
                    "summary": "Get product details (legacy)",
                    "parameters": [{"name": "product_id", "in": "path", "required": True, "schema": {"type": "integer"}}],
                    "responses": {"200": {"description": "Product details"}}
                }
            },
            "/api/v1/products": {
                "get": {
                    "summary": "Search and list products (v1 — recommended)",
                    "description": "Currency-aware product catalog with full filtering. Returns structured JSON with pricing, availability, images, and sizing.",
                    "parameters": [
                        {"name": "category", "in": "query", "schema": {"type": "string", "enum": ["supra","clubmaster","round","rectangle","rimless","square","cateye","aviator"]}},
                        {"name": "color", "in": "query", "schema": {"type": "string"}, "description": "Filter by color (partial match)"},
                        {"name": "shape", "in": "query", "schema": {"type": "string"}},
                        {"name": "size", "in": "query", "schema": {"type": "string", "enum": ["Small","Medium","Large","Extra Large"]}, "description": "Face fit"},
                        {"name": "min_price", "in": "query", "schema": {"type": "number"}},
                        {"name": "max_price", "in": "query", "schema": {"type": "number"}},
                        {"name": "in_stock", "in": "query", "schema": {"type": "string", "default": "true"}},
                        {"name": "q", "in": "query", "schema": {"type": "string"}, "description": "Free text search"},
                        {"name": "currency", "in": "query", "schema": {"type": "string", "enum": ["INR","EUR"], "default": "INR"}, "description": "Pricing currency. Also accepts Accept-Currency header."},
                        {"name": "page", "in": "query", "schema": {"type": "integer", "default": 1}},
                        {"name": "per_page", "in": "query", "schema": {"type": "integer", "default": 50, "maximum": 200}}
                    ],
                    "responses": {"200": {"description": "Paginated product list with pricing and availability"}}
                }
            },
            "/api/v1/products/{product_id}": {
                "get": {
                    "summary": "Get full product details (v1)",
                    "description": "Returns complete product information including sizing, pricing in requested currency, images, and availability.",
                    "parameters": [
                        {"name": "product_id", "in": "path", "required": True, "schema": {"type": "integer"}},
                        {"name": "currency", "in": "query", "schema": {"type": "string", "enum": ["INR","EUR"], "default": "INR"}}
                    ],
                    "responses": {"200": {"description": "Full product details"}}
                }
            },
            "/api/v1/categories": {
                "get": {
                    "summary": "List product categories with counts",
                    "description": "Returns all product categories with product counts and price ranges.",
                    "parameters": [
                        {"name": "currency", "in": "query", "schema": {"type": "string", "enum": ["INR","EUR"], "default": "INR"}}
                    ],
                    "responses": {"200": {"description": "List of categories"}}
                }
            }
        }
    }
    response = make_response(jsonify(spec))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response






# ===== REST API v1 — MACHINE COMMERCE =====

@bp.route('/api/v1/products', methods=['GET'])
def api_v1_products():
    """Enhanced product catalog API with currency support for AI agents."""
    db = get_db()
    cursor = db.cursor()
    
    # Parse query params
    category = request.args.get('category')
    color = request.args.get('color')
    shape = request.args.get('shape')
    size = request.args.get('size')  # face fit: Small, Medium, Large, Extra Large
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    in_stock = request.args.get('in_stock', 'true')
    q = request.args.get('q', '').strip()
    currency = request.args.get('currency', request.headers.get('Accept-Currency', 'INR')).upper()
    if currency not in ('INR', 'EUR'):
        currency = 'INR'
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)
    
    # Build query
    conditions = ["product_image IS NOT NULL", "product_image != ''", "product_image != 'NULL'"]
    params = []
    
    if in_stock.lower() == 'true':
        conditions.append("product_quantity > 0")
    
    if category:
        shape_col_map = {
            'supra': 'product_category_supra',
            'clubmaster': 'product_category_clubmaster',
            'round': 'product_category_round',
            'rectangle': 'product_category_rectangle',
            'rimless': 'product_category_rimless',
            'square': 'product_category_square',
            'cateye': 'product_category_cateye',
            'aviator': 'product_category_aviator',
            'oval': 'product_category_oval',
            'wayfarer': 'product_category_wayfarer',
        }
        col = shape_col_map.get(category.lower())
        if col:
            conditions.append(f"{col} = 'yes'")
    
    if color:
        conditions.append("(product_color LIKE %s OR product_primary_color LIKE %s)")
        params.extend([f'%{color}%', f'%{color}%'])
    
    if shape:
        conditions.append("product_shape LIKE %s")
        params.append(f'%{shape}%')
    
    if size:
        conditions.append("product_perception_value = %s")
        params.append(size.upper())
    
    if currency == 'EUR':
        if min_price is not None:
            conditions.append("product_special_price_eur >= %s")
            params.append(min_price)
        if max_price is not None:
            conditions.append("product_special_price_eur <= %s")
            params.append(max_price)
    else:
        if min_price is not None:
            conditions.append("product_special_price >= %s")
            params.append(min_price)
        if max_price is not None:
            conditions.append("product_special_price <= %s")
            params.append(max_price)
    
    if q:
        conditions.append("(product_name LIKE %s OR product_code LIKE %s OR product_color LIKE %s)")
        params.extend([f'%{q}%', f'%{q}%', f'%{q}%'])
    
    where = ' AND '.join(conditions)
    
    # Get total count
    cursor.execute(f"SELECT COUNT(*) FROM products WHERE {where}"
                   + catalogue_site_filter(), params)
    row = cursor.fetchone(); total = list(row.values())[0] if isinstance(row, dict) else row[0] if row else 0
    if isinstance(total, dict):
        total = list(total.values())[0]
    
    # Get paginated results
    offset = (page - 1) * per_page
    cursor.execute(f"""
        SELECT product_id, product_name, product_code, product_category, product_color,
               product_primary_color, product_secondary_color,
               product_size, product_bridge, product_diameter, product_lenght,
               product_material, product_shape, product_perception_value,
               product_price, product_special_price,
               product_price_eur, product_special_price_eur,
               product_quantity, product_slug, product_details, product_gender,
               product_image
        FROM products WHERE {where}
        {catalogue_site_filter()}
        ORDER BY product_id DESC
        LIMIT %s OFFSET %s
    """, params + [per_page, offset])
    
    rows = cursor.fetchall()
    cursor.close()
    
    products = []
    for r in rows:
        if isinstance(r, dict):
            row = r
        else:
            row = {
                'product_id': r[0], 'product_name': r[1], 'product_code': r[2],
                'product_category': r[3], 'product_color': r[4],
                'product_primary_color': r[5], 'product_secondary_color': r[6],
                'product_size': r[7], 'product_bridge': r[8], 'product_diameter': r[9],
                'product_lenght': r[10], 'product_material': r[11], 'product_shape': r[12],
                'product_perception_value': r[13], 'product_price': r[14],
                'product_special_price': r[15], 'product_price_eur': r[16],
                'product_special_price_eur': r[17], 'product_quantity': r[18],
                'product_slug': r[19], 'product_details': r[20], 'product_gender': r[21],
                'product_image': r[22]
            }
        
        price = float(row['product_special_price_eur']) if currency == 'EUR' and row.get('product_special_price_eur') else float(row['product_special_price'] or 0)
        mrp = float(row['product_price_eur']) if currency == 'EUR' and row.get('product_price_eur') else float(row['product_price'] or 0)
        
        eu_prefix = '/eu' if currency == 'EUR' else ''
        slug = row.get('product_slug', '')
        cat = row.get('product_category', '').lower().replace(' ', '-')
        
        images = []
        if row.get('product_image'):
            for img in str(row['product_image']).split(','):
                img = img.strip()
                if img and img != 'NULL':
                    images.append(f"https://optiwar.com/static/{img}")
        
        products.append({
            'id': row['product_id'],
            'name': row['product_name'],
            'code': row['product_code'],
            'description': row.get('product_details') or '',
            'category': row['product_category'],
            'color': row['product_color'],
            'primary_color': row.get('product_primary_color'),
            'secondary_color': row.get('product_secondary_color'),
            'material': row.get('product_material'),
            'shape': row.get('product_shape'),
            'gender': row.get('product_gender', 'Unisex'),
            'face_fit': row.get('product_perception_value'),
            'size': {
                'lens_width_mm': row.get('product_diameter'),
                'bridge_width_mm': row.get('product_bridge'),
                'temple_length_mm': row.get('product_lenght'),
                'size_string': row.get('product_size'),
            },
            'pricing': {
                'currency': currency,
                'price': price,
                'mrp': mrp,
                'savings': round(mrp - price, 2) if mrp > price else 0,
                'includes': 'Free complimentary single-vision prescription lenses, free delivery, GST included'
            },
            'availability': {
                'in_stock': (row.get('product_quantity') or 0) > 0,
                'quantity': row.get('product_quantity', 0)
            },
            'images': images,
            'url': f"https://optiwar.com{eu_prefix}/categories/{cat}/{slug}?pid={row['product_id']}",
        })
    
    result = {
        'status': 'ok',
        'currency': currency,
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'total_pages': (total + per_page - 1) // per_page
        },
        'products': products
    }
    
    response = make_response(jsonify(result))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Cache-Control'] = 'public, max-age=300'
    return response


@bp.route('/api/v1/products/<int:product_id>', methods=['GET'])
def api_v1_product_detail(product_id):
    """Single product detail with full specs and currency support."""
    db = get_db()
    cursor = db.cursor()
    currency = request.args.get('currency', request.headers.get('Accept-Currency', 'INR')).upper()
    if currency not in ('INR', 'EUR'):
        currency = 'INR'
    
    cursor.execute("SELECT * FROM products WHERE product_id = %s", (product_id,))
    row = cursor.fetchone()
    cursor.close()
    
    if not row or not is_product_allowed(row):
        return jsonify({'status': 'error', 'message': 'Product not found'}), 404
    
    price = float(row['product_special_price_eur']) if currency == 'EUR' and row.get('product_special_price_eur') else float(row['product_special_price'] or 0)
    mrp = float(row['product_price_eur']) if currency == 'EUR' and row.get('product_price_eur') else float(row['product_price'] or 0)
    
    eu_prefix = '/eu' if currency == 'EUR' else ''
    slug = row.get('product_slug', '')
    cat = row.get('product_category', '').lower().replace(' ', '-')
    
    images = []
    if row.get('product_image'):
        for img in str(row['product_image']).split(','):
            img = img.strip()
            if img and img != 'NULL':
                images.append(f"https://optiwar.com/static/{img}")
    
    product = {
        'id': row['product_id'],
        'name': row['product_name'],
        'code': row['product_code'],
        'description': row.get('product_details') or '',
        'category': row['product_category'],
        'color': row['product_color'],
        'primary_color': row.get('product_primary_color'),
        'secondary_color': row.get('product_secondary_color'),
        'material': row.get('product_material'),
        'shape': row.get('product_shape'),
        'gender': row.get('product_gender', 'Unisex'),
        'face_fit': row.get('product_perception_value'),
        'size': {
            'lens_width_mm': row.get('product_diameter'),
            'bridge_width_mm': row.get('product_bridge'),
            'temple_length_mm': row.get('product_lenght'),
            'size_string': row.get('product_size'),
        },
        'pricing': {
            'currency': currency,
            'price': price,
            'mrp': mrp,
            'savings': round(mrp - price, 2) if mrp > price else 0,
            'includes': 'Free complimentary single-vision prescription lenses, free delivery, GST included'
        },
        'availability': {
            'in_stock': (row.get('product_quantity') or 0) > 0,
            'quantity': row.get('product_quantity', 0)
        },
        'images': images,
        'url': f"https://optiwar.com{eu_prefix}/categories/{cat}/{slug}?pid={row['product_id']}",
    }
    
    response = make_response(jsonify({'status': 'ok', 'currency': currency, 'product': product}))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


@bp.route('/api/v1/products/<code>/media', methods=['GET'])
def api_v1_product_media(code):
    """AI-friendly, server-owned media metadata for a product (by product_code).

    Clients must NOT reconstruct filenames: every URL (versioned master, zoom,
    and the AVIF/WebP/JPEG responsive ladders) is emitted here. Angles are
    deduped by content hash and labelled with a view type + descriptive alt.
    Follows the frozen media contract (media_schema)."""
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT product_id, product_code, product_name, product_image,
               product_category, product_slug, color_display, product_color,
               product_vertical, sell_on_com, sell_on_in
        FROM products WHERE product_code = %s LIMIT 1
    """, (code,))
    row = cur.fetchone()
    cur.close()
    if not row or not is_product_allowed(row):
        return jsonify({'status': 'error', 'message': 'Product not found'}), 404

    is_india = _req_is_india()
    base = 'https://optiwar.in' if is_india else 'https://optiwar.com'
    cat = (row.get('product_category') or '').lower().replace(' ', '-')
    canonical = '%s/categories/%s/%s?pid=%s' % (base, cat, row.get('product_slug'), row['product_id'])
    color = row.get('color_display') or row.get('product_color') or ''
    name = ('%s %s' % (row.get('product_name') or '', row.get('product_code') or '')).strip()

    # Dedupe angles by content hash while keeping the original path for the ladders.
    seen, entries = set(), []
    for entry in (row.get('product_image') or '').split(','):
        e = entry.strip()
        if not e:
            continue
        v = versioned_image_url(e, '')
        if not v:
            continue
        k = v.split('?v=', 1)[1] if '?v=' in v else v
        if k in seen:
            continue
        seen.add(k)
        entries.append(e)

    _abs = lambda s: s.replace('/static/./', '/static/').replace('/static/', base + '/static/') if s else s
    view_labels = ['front', 'angle', 'side', 'temple-detail', 'top', 'folded', 'lens', 'hinge', 'case', 'detail']
    media = []
    for idx, e in enumerate(entries):
        m = build_media_one(e)  # frozen contract: src, zoom, has_derivatives, avif, webp, jpg
        if not m:
            continue
        view = view_labels[idx] if idx < len(view_labels) else 'other'
        media.append({
            'order': idx,
            'view': view,
            'representative_of_page': idx == 0,
            'master': versioned_image_url(e, base),      # canonical versioned JPEG
            'src': _abs(m['src']),
            'zoom': _abs(m['zoom']),
            'has_derivatives': m['has_derivatives'],
            'srcset': {
                'avif': _abs(m['avif']),
                'webp': _abs(m['webp']),
                'jpg': _abs(m['jpg']),
            },
            'derivative_widths': [200, 400, 800, 1200, 2000] if m['has_derivatives'] else [],
            'alt': '%s \u2014 %s view%s' % (name, view.replace('-', ' '), (' in ' + color) if color else ''),
        })

    payload = {
        'status': 'ok',
        'media_schema': MEDIA_SCHEMA_VERSION,
        'product': {
            'id': row['product_id'],
            'code': row['product_code'],
            'name': row.get('product_name'),
            'color': color or None,
            'canonical_url': canonical,
        },
        'primary_image': media[0]['master'] if media else None,
        'image_count': len(media),
        'fallback_order': ['avif', 'webp', 'jpg'],
        'media': media,
    }
    response = make_response(jsonify(payload))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response


@bp.route('/api/v1/categories', methods=['GET'])
def api_v1_categories():
    """List product categories with counts and pricing."""
    db = get_db()
    cursor = db.cursor()
    currency = request.args.get('currency', request.headers.get('Accept-Currency', 'INR')).upper()
    if currency not in ('INR', 'EUR'):
        currency = 'INR'
    
    cats = [
        ('supra', 'Half Frame (Supra)', 'product_category_supra'),
        ('clubmaster', 'Clubmaster', 'product_category_clubmaster'),
        ('round', 'Round', 'product_category_round'),
        ('rectangle', 'Rectangle', 'product_category_rectangle'),
        ('rimless', 'Rimless / 3-Piece', 'product_category_rimless'),
        ('square', 'Square', 'product_category_square'),
        ('cateye', 'Cat Eye', 'product_category_cateye'),
        ('aviator', 'Aviator', 'product_category_aviator'),
    ]
    
    price_col = 'product_special_price_eur' if currency == 'EUR' else 'product_special_price'
    
    categories = []
    for slug, name, col in cats:
        cursor.execute(f"""
            SELECT COUNT(*), MIN({price_col}), MAX({price_col})
            FROM products WHERE {col} = 'yes' AND product_quantity > 0
        """)
        row = cursor.fetchone()
        count = row[0] if isinstance(row, tuple) else list(row.values())[0]
        min_p = row[1] if isinstance(row, tuple) else list(row.values())[1]
        max_p = row[2] if isinstance(row, tuple) else list(row.values())[2]
        
        categories.append({
            'slug': slug,
            'name': name,
            'product_count': count,
            'price_range': {
                'currency': currency,
                'min': float(min_p) if min_p else 0,
                'max': float(max_p) if max_p else 0
            },
            'url': f"https://optiwar.com/{('eu/' if currency == 'EUR' else '')}categories/{slug}",
            'api_url': f"https://optiwar.com/api/v1/products?category={slug}&currency={currency}"
        })
    
    cursor.close()
    
    response = make_response(jsonify({
        'status': 'ok',
        'currency': currency,
        'categories': categories
    }))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@bp.route('/guides/frame-shapes')
def guide_frame_shapes():
    """Frame shape guide page with live product counts."""
    db = get_db()
    cursor = db.cursor()
    
    style_configs = [
        ('supra', 'Half Frame (Supra)', 'product_category_supra', 'Everyday wear, lightweight, minimalist'),
        ('clubmaster', 'Clubmaster', 'product_category_clubmaster', 'Retro-classic, professional, unisex'),
        ('round', 'Round', 'product_category_round', 'Vintage look, oval/square faces'),
        ('rectangle', 'Rectangle', 'product_category_rectangle', 'Professional, round faces'),
        ('rimless', 'Rimless / 3-Piece', 'product_category_rimless', 'Ultra-lightweight, nearly invisible'),
        ('square', 'Square', 'product_category_square', 'Bold/modern look, round/oval faces'),
    ]
    
    styles = []
    for slug, name, col, best_for in style_configs:
        cursor.execute(f"""
            SELECT COUNT(*), MIN(product_special_price), MIN(product_special_price_eur)
            FROM products 
            WHERE {col} = 'yes' AND product_quantity > 0
        """)
        row = cursor.fetchone()
        count = row[0] if isinstance(row, tuple) else row['COUNT(*)']
        min_price = row[1] if isinstance(row, tuple) else row['MIN(product_special_price)']
        min_eur = row[2] if isinstance(row, tuple) else row['MIN(product_special_price_eur)']
        styles.append({
            'slug': slug,
            'name': name,
            'count': count,
            'min_price': int(min_price) if min_price else 499,
            'min_price_eur': f"{min_eur:.2f}" if min_eur else "5.49",
            'best_for': best_for
        })
    
    cursor.close()
    return render_template('guide_frame_shapes.html', styles=styles)

@bp.route('/.well-known/ucp')
def ucp_discovery():
    """Universal Commerce Protocol discovery endpoint for AI agent commerce."""
    ucp = {
        "ucp": {
            "version": "2026-04-08",
            "business": {
                "name": "Optiwar",
                "description": "India's wholesale optical e-commerce — spectacle frames with free prescription lenses at factory outlet prices",
                "url": "https://optiwar.com",
                "logo": "https://optiwar.com/static/favicon-512x512.png",
                "contact_email": "support@optiwar.com"
            },
            "services": {
                "dev.ucp.shopping": [{
                    "version": "2026-04-08",
                    "transport": "rest",
                    "endpoint": "https://optiwar.com/api"
                }]
            },
            "capabilities": {
                "dev.ucp.shopping.catalog.search": [{"version": "2026-04-08"}],
                "dev.ucp.shopping.catalog.lookup": [{"version": "2026-04-08"}],
                "dev.ucp.shopping.catalog.categories": [{"version": "2026-04-08"}]
            },
            "supported_currencies": ["INR", "EUR"],
            "supported_regions": ["IN", "EU"],
            "documentation": {
                "openapi": "https://optiwar.com/api/openapi.json",
                "ai_plugin": "https://optiwar.com/.well-known/ai-plugin.json",
                "llms_txt": "https://optiwar.com/llms.txt"
            }
        }
    }
    response = make_response(jsonify(ucp))
    response.headers['Content-Type'] = 'application/json'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Cache-Control'] = 'public, max-age=3600'
    return response

@bp.route('/llms.txt')
def llms_txt():
    """Plain-text file for LLMs describing how to interact with Optiwar."""
    text = """# Optiwar
> Factory outlet eyewear by Krishna Ecommerce Technologies Pvt Ltd. Premium spectacle frames with free prescription lenses at wholesale pricing. 998+ frames in stock.

## Two Sites
- https://optiwar.com — Global customers, EUR (€) pricing, ships worldwide (except USA & India)
- https://optiwar.in — Indian customers, INR (₹) pricing, ships within India

## About
- Factory outlet pricing — save up to 70% vs retail optical stores
- Free complimentary single-vision lenses with every frame
- Lens upgrades: Anti-Glare (₹50/€5), Blue Cut (₹100/€7), Multi Anti-Glare (₹200/€7), Polarized (₹800/€10), Thin (₹100/€10), Extra-Thin (₹350/€7), Photo-Grey (₹350/€10), Photo-Brown (₹650/€10), Bifocal KT (₹250/€8), Bifocal D (₹500/€8), Progressive (₹1000/€10)
- Categories: Supra (Half Frame), Clubmaster, Round, Rectangle, Rimless, Square
- Ships globally via FedEx / DHL — free shipping, no minimums
- Support via ticketing only — 24hr response time

## Product Catalog API
- Product catalog: GET /api/v1/products (supports ?currency=INR or EUR)
- Search: GET /api/v1/products?q={query}&category={cat}&color={color}&shape={shape}&min_price={min}&max_price={max}&in_stock=true
- Product detail: GET /api/v1/products/{id}?currency=INR or EUR
- Categories API: GET /api/v1/categories?currency=INR or EUR
- Categories: supra, clubmaster, round, rectangle, rimless, square
- OpenAPI spec: /api/openapi.json
- AI plugin: /.well-known/ai-plugin.json
- UCP discovery: /.well-known/ucp

## AI Knowledge & FAQ
- FAQ (human-readable + JSON-LD): /ai-faq
- Knowledge Base JSON (machine-readable Q&A): /ai-knowledge-base.json
- Order Schema JSON (fields + decision rules): /ai-order-schema.json
- Lens Rules JSON (decision tree for lens recommendations): /ai-lens-rules.json
- Frame Rules JSON (face measurement to frame sizing): /ai-frame-rules.json
- Prescription Rules JSON (validation + field definitions): /ai-prescription-rules.json
- AI discovery: /ai.txt

## Pricing
- Frames: ₹499–₹999 (INR) / €5–€11 (EUR)
- Free shipping on all orders — no minimums worldwide

## Shipping
- Ships from Gurgaon, India within 24-36 hours (complex Rx: 3-7 days)
- Delivery: 3-7 business days globally via FedEx/DHL
- Customer pays import duties/taxes in their country
- No shipping to USA (FDA regulations) or India via optiwar.com (use optiwar.in)

## Prescription
- All powers fulfilled from factory outlet
- Required: Right SPH, Left SPH
- Optional: CYL, AXIS (required if CYL present), ADD (for bifocal/progressive), PD, Prism
- SPH (Sphere): main correction power. Negative = myopia, positive = hyperopia
- CYL (Cylinder): astigmatism correction. Requires AXIS (0-180°) when present
- ADD (Addition): near/reading power for bifocal/progressive lenses (age 40+)
- PD: Pupillary Distance — single (e.g. 63mm) or dual (e.g. R:31.5 L:32.0)
- Prism: eye alignment correction (uncommon, requires image upload)
- Face measurement tool: /tryon (measures PD and face width via camera)
- Prescription upload accepted (image/scan)
- Full validation rules: /ai-prescription-rules.json

## Payments
- Visa, Mastercard, American Express, Netbanking, Apple Pay
- INR via Paytm (optiwar.in), EUR via Razorpay (optiwar.com)

## Returns & Cancellation
- Cancel within 6hrs free; processed orders up to 50% fee
- International: 7% unprocessed / 57% processed cancellation fee
- Returns subjective — factory outlet pricing, no warranty
- Refunds to original payment method in 3-5 business days

## AI Guides
- PD Measurement: https://optiwar.com/ai-guide/pd-measurement
- Prescription Reading: https://optiwar.com/ai-guide/prescription-reading
- Frame Shapes: https://optiwar.com/guides/frame-shapes

## AI API Endpoints
- Natural Language Q&A: POST /api/ai/answer {{"question": "..."}}
- Frame Recommendation: POST /api/ai/recommend-frame {{"face_width_mm": 137, "pd_mm": 66.5}}
- Lens Recommendation: POST /api/ai/recommend-lens {{"use_cases": ["computer"], "sph_right": -2.50}}

## Key Pages
- Homepage: https://optiwar.com/
- All Frames: https://optiwar.com/eyeglasses/all-spectacle-frames.html
- Supra: https://optiwar.com/categories/supra
- Clubmaster: https://optiwar.com/categories/clubmaster
- Round: https://optiwar.com/categories/round
- Rectangle: https://optiwar.com/categories/rectangle
- Rimless: https://optiwar.com/categories/rimless
- Square: https://optiwar.com/categories/square
- Lenses Guide: https://optiwar.com/lenses
- Frame Shapes Guide: https://optiwar.com/guides/frame-shapes
- Face Measurement: https://optiwar.com/tryon
- FAQ: https://optiwar.com/ai-faq
- Terms: https://optiwar.com/terms_and_conditions
- Privacy: https://optiwar.com/privacy_policy
- Sitemap: https://optiwar.com/sitemap.xml

## Contact
- Support: via ticketing system (24hr response)
- Email: support@optiwar.com
- Instagram: @optiwar_
- Address: Godown Village Fazilpur, Badshahpur Tethar(222), Badshahpur, Gurgaon-122101 India
"""
    if _req_is_india():
        text = text.replace('https://optiwar.com/', 'https://optiwar.in/')
    response = make_response(text)
    response.headers['Content-Type'] = 'text/plain; charset=utf-8'
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


# ===== AI FAQ & KNOWLEDGE ENDPOINTS =====

@bp.route('/ai-faq')
def ai_faq():
    """Human-readable FAQ page with JSON-LD FAQPage schema for AEO/SEO."""
    is_india = _req_is_india()
    site_url = 'https://optiwar.in' if is_india else 'https://optiwar.com'
    c = '\u20b9' if is_india else '\u20ac'

    faq_sections = [
        {
            'id': 'business', 'label': 'About Optiwar', 'title': 'Business Identity',
            'questions': [
                {'q': 'What is Optiwar?', 'a': f'Optiwar is a factory outlet eyewear e-commerce platform operated by Krishna Ecommerce Technologies Private Limited. We sell premium spectacle frames with complimentary prescription lenses at wholesale factory outlet prices \u2014 up to 70% less than retail optical stores. We operate two sites: <a href="https://optiwar.com">optiwar.com</a> for global customers (\u20ac pricing) and <a href="https://optiwar.in">optiwar.in</a> for Indian customers (\u20b9 pricing).'},
                {'q': 'Who operates Optiwar?', 'a': 'Krishna Ecommerce Technologies Private Limited, located at Godown Village Fazilpur, Badshahpur Tethar(222), Badshahpur, Gurgaon-122101 India. All products ship from India worldwide.'},
                {'q': 'What is the difference between optiwar.com and optiwar.in?', 'a': '<a href="https://optiwar.com">optiwar.com</a> serves global/international customers with EUR (\u20ac) pricing. <a href="https://optiwar.in">optiwar.in</a> serves Indian customers with INR (\u20b9) pricing. Both offer the same catalog and ship from India. optiwar.com does not ship to the USA or India.'},
                {'q': 'How do I contact customer support?', 'a': 'Optiwar provides support exclusively through the ticketing system. Submit a request via the contact form on the website. Expect a response within 24 hours. There is no phone support.'},
                {'q': 'How does Optiwar offer such low prices?', 'a': 'Optiwar operates as a factory outlet, selling directly from the factory at wholesale prices. By eliminating retail middlemen, store overhead, and traditional optical shop markup, we offer savings of up to 70%. Every frame includes complimentary prescription lenses.'},
                {'q': 'Does Optiwar have a physical store?', 'a': 'Optiwar operates primarily as an online factory outlet. Our warehouse and fulfilment centre is at Godown Village Fazilpur, Badshahpur Tethar(222), Badshahpur, Gurgaon-122101 India.'},
                {'q': 'How do I create an account?', 'a': 'Register with email and password, or sign in instantly with Google. Registration is free and gives you access to order history, saved addresses, and saved prescriptions.'},
            ]
        },
        {
            'id': 'products', 'label': 'Products', 'title': 'Product Categories',
            'questions': [
                {'q': 'What product categories does Optiwar offer?', 'a': f'Optiwar offers: (1) <strong>Spectacle Frames</strong> \u2014 998+ frames in Supra, Clubmaster, Round, Rectangle, Rimless, and Square styles ({c}499\u2013{c}999 INR / \u20ac5\u2013\u20ac11 EUR). (2) <strong>Contact Lenses</strong>. (3) <strong>Hearing Aids</strong>.'},
                {'q': 'What spectacle frame styles are available?', 'a': 'Supra (Half Frame), Clubmaster, Round, Rectangle, Rimless, and Square. Browse all at <a href="' + site_url + '/eyeglasses/all-spectacle-frames.html">All Frames</a>.'},
                {'q': 'Are frames suitable for all ages?', 'a': 'Yes \u2014 frames come in lens widths from 42mm to 58mm+, suitable for teens through adults. Use the size guide to find the right fit.'},
                {'q': 'Can I order prescription sunglasses?', 'a': f'Yes. Select any frame and add Polarized lenses ({c}800/\u20ac10) or Photochromic lenses (from {c}350/\u20ac10) to create prescription sunglasses.'},
            ]
        },
        {
            'id': 'frames', 'label': 'Frames & Sizing', 'title': 'Frame Size & Selection',
            'questions': [
                {'q': 'What does frame size (e.g. 52-18-140) mean?', 'a': 'Three numbers: <strong>Lens Width \u2013 Bridge Width \u2013 Temple Length</strong>, all in mm. Example: 52-18-140 means 52mm lens, 18mm bridge, 140mm temple arm.'},
                {'q': 'How do I choose the right frame size?', 'a': f'Use Optiwar\'s <a href="{site_url}/tryon">face measurement tool</a> \u2014 it uses your camera to measure face width and recommend suitable sizes. Or measure an existing pair (size is printed inside the temple arm).'},
                {'q': 'What frame shapes suit my face?', 'a': f'Round faces suit angular frames (Rectangle, Square). Oval faces suit most styles. Square faces suit round/curved frames. Heart-shaped faces suit wider-bottom frames. Try the <a href="{site_url}/guides/frame-shapes">Frame Shapes Guide</a>.'},
            ]
        },
        {
            'id': 'lenses', 'label': 'Lenses', 'title': 'Prescription Lenses',
            'questions': [
                {'q': 'Does Optiwar include lenses with frames?', 'a': f'Yes. Every frame includes <strong>free single-vision prescription lenses</strong>. You can upgrade to premium types for an additional charge. See all lens types at <a href="{site_url}/lenses">Lenses Guide</a>.'},
                {'q': 'What lens types are available?', 'a': f'12 types: Anti-Glare ({c}50/\u20ac5), Blue Cut ({c}100/\u20ac7), Multi Anti-Glare ({c}200/\u20ac7), Polarized ({c}800/\u20ac10), Thin ({c}100/\u20ac10), Extra-Thin ({c}350/\u20ac7), Photo-Grey ({c}350/\u20ac10), Photo-Brown ({c}650/\u20ac10), Bifocal KT ({c}250/\u20ac8), Bifocal D ({c}500/\u20ac8), Progressive ({c}1000/\u20ac10), plus complimentary single-vision.'},
                {'q': 'What is the complimentary lens?', 'a': 'Free standard-index single-vision lenses with your prescription. You only pay extra for upgrades (coatings, thickness, bifocal/progressive).'},
                {'q': 'Can I combine lens upgrades?', 'a': 'Yes. For example, Blue Cut + Thin, or Progressive + Anti-Glare. Our team evaluates every prescription and may provide upgraded material at their own cost if needed for proper fitting.'},
            ]
        },
        {
            'id': 'lens-rec', 'label': 'Lens Recommendations', 'title': 'Lens Recommendation Rules',
            'questions': [
                {'q': 'Blue Cut vs Anti-Glare \u2014 which should I choose?', 'a': f'<strong>Blue Cut</strong> is better for heavy screen users \u2014 it blocks harmful blue-violet light (380\u2013450nm). Anti-Glare is a basic upgrade for general use and night driving. For computer/phone users, choose Blue Cut. Details: <a href="{site_url}/lenses/blue-cut-lenses">Blue Cut Lenses</a>.'},
                {'q': 'When should I choose Thin vs Extra-Thin lenses?', 'a': 'Most prescriptions work with <strong>Thin</strong> (1.56\u20131.61 index). <strong>Extra-Thin</strong> (1.67+) is recommended for higher powers (\u00b14 and above) for better aesthetics and lighter weight. Our team may upgrade at their cost if needed.'},
                {'q': 'What lens is best for driving?', 'a': f'<strong>Polarized</strong> for daytime (eliminates road glare). <strong>Anti-Glare</strong> for night driving (reduces headlight reflections). <strong>Photochromic</strong> for variable conditions.'},
                {'q': 'What lens for outdoor activities?', 'a': '<strong>Polarized</strong> eliminates surface glare from water/snow/roads. <strong>Photochromic</strong> (Photo-Grey or Photo-Brown) auto-darkens in sunlight and clears indoors.'},
                {'q': 'Photo-Grey vs Photo-Brown?', 'a': 'Purely aesthetic. Both darken in sunlight and clear indoors the same way. Choose the colour that complements your frame and style.'},
                {'q': 'Progressive vs Bifocal?', 'a': f'<strong>Progressive</strong> is the modern, advanced option \u2014 seamless no-line multifocal for distance, intermediate, and near. <strong>Bifocal</strong> is older-gen with a visible line and only two zones. We recommend Progressive. Details: <a href="{site_url}/lenses/progressive-lenses">Progressive Lenses</a>.'},
                {'q': 'I need glasses for both distance and reading?', 'a': 'Choose <strong>Progressive lenses</strong> for seamless multi-distance correction without visible lines. Bifocal (KT or D) is available as a traditional alternative.'},
                {'q': 'Best lens for high prescriptions?', 'a': '<strong>Extra-Thin</strong> (1.67+ index) \u2014 thinner, lighter, and better-looking for high powers.'},
            ]
        },
        {
            'id': 'prescription', 'label': 'Prescriptions', 'title': 'Prescription & PD',
            'questions': [
                {'q': 'What prescription details do I need?', 'a': '<strong>Required:</strong> Right eye SPH, Left eye SPH. <strong>Optional:</strong> CYL (if astigmatism), AXIS (required when CYL present), ADD (for bifocal/progressive), PD. You can also upload a prescription image.'},
                {'q': 'How do I read my prescription?', 'a': '<strong>SPH</strong> = main power (\u2212 near-sighted, + far-sighted). <strong>CYL</strong> = astigmatism correction. <strong>AXIS</strong> = angle of CYL (0\u2013180\u00b0). <strong>ADD</strong> = reading addition. <strong>PD</strong> = distance between pupils in mm.'},
                {'q': 'What power range does Optiwar fulfil?', 'a': 'All prescriptions are fulfilled from the factory outlet. Very high powers may incur additional charges for specialised manufacturing.'},
                {'q': 'Is PD mandatory?', 'a': f'Not mandatory but strongly recommended. Use the <a href="{site_url}/tryon">face measurement tool</a> to measure your PD with your camera for better fitting comfort.'},
                {'q': 'Can I upload my prescription as an image?', 'a': 'Yes. During prescription entry, upload a photo or scan. Our team reads the values and configures the correct lenses.'},
                {'q': 'What does AXIS mean?', 'a': 'AXIS is the angle (0\u2013180\u00b0) for astigmatism correction. Required only when CYL is present. Our system validates this automatically.'},
                {'q': 'What is ADD power?', 'a': 'ADD (addition) is extra magnification for reading, used in bifocal/progressive lenses. Typically +0.75 to +3.00. Needed only for bifocal or progressive orders.'},
                {'q': 'Does Optiwar verify prescriptions?', 'a': 'Yes. Our software validates parameters automatically. Our team also reviews each order and makes corrective fitting decisions if needed \u2014 upgrading lens material at their own cost when required.'},
                {'q': 'Do you accept expired prescriptions?', 'a': 'We are an online fulfilment factory outlet \u2014 we consider your prescription in order when you place the order. We recommend regular eye checks every 1\u20132 years.'},
                {'q': 'Extra charges for high prescriptions?', 'a': 'Standard prescriptions are fulfilled with complimentary lenses at no extra charge. Very high powers may attract additional manufacturing charges. Our team may upgrade material at their cost if needed.'},
            ]
        },
        {
            'id': 'shipping', 'label': 'Shipping', 'title': 'Shipping & Delivery',
            'questions': [
                {'q': 'Where does Optiwar ship to?', 'a': '<strong>optiwar.in</strong> ships within India. <strong>optiwar.com</strong> ships globally except USA (FDA regulations) and India (use optiwar.in). All products ship from Gurgaon, India.'},
                {'q': 'Why no shipping to the USA?', 'a': 'Due to potential US FDA detentions on eyewear imports. In the interest of site visitors, US shipping is paused. This may change in the future.'},
                {'q': 'How long does shipping take?', 'a': 'Most orders ship within <strong>24\u201336 hours</strong>. Complex prescriptions: 3\u20137 days. Once shipped, delivery is <strong>3\u20137 business days</strong> globally via FedEx/DHL.'},
                {'q': 'Which carriers are used?', 'a': '<strong>FedEx</strong> and <strong>DHL</strong> for global shipping. AWB tracking shared via email once shipped.'},
                {'q': 'Is shipping free?', 'a': '<strong>Yes \u2014 100% free</strong> on all orders, no minimum. Applies to both domestic (India) and international.'},
                {'q': 'Can I track my order?', 'a': 'Yes. Once shipped, you receive an AWB tracking number via email. Track on FedEx or DHL website.'},
            ]
        },
        {
            'id': 'customs', 'label': 'Customs', 'title': 'Customs & Duties',
            'questions': [
                {'q': 'Do I pay customs duties on international orders?', 'a': 'Yes. Customers pay all import duties, taxes, and customs fees in their country. Rates vary by country and are beyond Optiwar\'s scope to advise.'},
                {'q': 'What if my order is held at customs?', 'a': 'International orders once shipped are subject to the importing country\'s customs rules. Any detention is the customer\'s liability per RBI rules. Track via AWB number.'},
            ]
        },
        {
            'id': 'returns', 'label': 'Returns', 'title': 'Returns & Cancellation',
            'questions': [
                {'q': 'Can I cancel my order?', 'a': '<strong>Within 6 hours</strong> (unprocessed) \u2014 free cancellation. <strong>Processed orders</strong> \u2014 up to 50% cancellation fee. <strong>International unprocessed</strong> \u2014 7% fee. <strong>International processed</strong> \u2014 57% fee. Submit via contact form.'},
                {'q': 'What is the return policy?', 'a': 'Returns are subjective at factory outlet pricing. Product must be returned to Optiwar\'s location. Refund at 5% discount of product value. Used/damaged products not refunded. Customer bears return shipping. International orders cannot be returned once shipped.'},
                {'q': 'How long do refunds take?', 'a': 'Approved refunds process to the original payment method within <strong>3\u20135 business days</strong>.'},
                {'q': 'Can I return international orders?', 'a': 'No. International orders once shipped with AWB cannot be returned or refunded per RBI export rules.'},
                {'q': 'Is there a warranty?', 'a': 'Optiwar is a factory outlet with exceptional direct factory pricing. Factory outlet products do not include traditional warranty terms.'},
                {'q': 'What if I receive a damaged product?', 'a': 'Contact support immediately via the ticketing system. Handled case-by-case. Customer bears return shipping.'},
            ]
        },
        {
            'id': 'payment', 'label': 'Payment', 'title': 'Payment Methods',
            'questions': [
                {'q': 'What payment methods are accepted?', 'a': '<strong>Visa, Mastercard, American Express, Netbanking, Apple Pay.</strong> Options auto-adjust by country. INR payments via Paytm/Razorpay, EUR via Razorpay.'},
                {'q': 'What currencies are supported?', 'a': '<strong>INR (\u20b9)</strong> on optiwar.in and <strong>EUR (\u20ac)</strong> on optiwar.com.'},
                {'q': 'Is payment secure?', 'a': 'Yes. Processed through PCI-DSS compliant Paytm and Razorpay gateways. Optiwar does not store card details.'},
            ]
        },
        {
            'id': 'ordering', 'label': 'Ordering', 'title': 'Order Process',
            'questions': [
                {'q': 'What is the step-by-step order process?', 'a': '1. Browse & select frame. 2. Add to cart. 3. Enter prescription. 4. Choose lens upgrades. 5. Checkout with delivery details. 6. Complete payment. 7. Receive email confirmation. 8. Order ships within 24\u201336hrs. 9. Track via AWB.'},
                {'q': 'Can I order without an account?', 'a': 'Guest checkout is available. Creating an account lets you track orders, save addresses, and reorder easily.'},
                {'q': 'Can I change my order after placing it?', 'a': 'Contact support ASAP via ticketing. Unprocessed orders (within 6hrs) have the best chance. Once lens processing begins, changes may not be possible.'},
                {'q': 'Can I view past orders?', 'a': f'Yes. Log in and visit <a href="{site_url}/my_orders">My Orders</a>.'},
                {'q': 'Can I save multiple delivery addresses?', 'a': 'Yes. Logged-in customers can save and manage multiple addresses in their profile.'},
            ]
        },
        {
            'id': 'ai-agent', 'label': 'AI Agents', 'title': 'AI Agent Integration',
            'questions': [
                {'q': 'What API endpoints are available for AI agents?', 'a': 'GET /api/v1/products (catalog), GET /api/v1/products/{id} (detail), GET /api/v1/categories, GET /api/openapi.json (spec), GET /.well-known/ai-plugin.json (plugin), GET /.well-known/ucp (commerce protocol). All support ?currency=INR or EUR.'},
                {'q': 'Can an AI agent place an order?', 'a': 'AI agents can collect all order info (frame, Rx, address, contact) and submit for processing. Payment authorisation must come directly from the customer.'},
                {'q': 'What machine-readable files are available?', 'a': f'<a href="{site_url}/ai-knowledge-base.json">Knowledge Base JSON</a> (89 Q&A pairs), <a href="{site_url}/ai-order-schema.json">Order Schema JSON</a>, <a href="{site_url}/ai-lens-rules.json">Lens Rules JSON</a> (decision tree), <a href="{site_url}/ai-frame-rules.json">Frame Rules JSON</a> (sizing), <a href="{site_url}/ai-prescription-rules.json">Prescription Rules JSON</a> (validation), <a href="{site_url}/llms.txt">llms.txt</a>, <a href="{site_url}/ai.txt">ai.txt</a>.'},
            ]
        },
        {
            'id': 'escalation', 'label': 'Escalation', 'title': 'Escalation Rules',
            'questions': [
                {'q': 'When should an AI agent escalate to a human?', 'a': 'Escalate for: complex prescription queries, order modifications/cancellations, complaints/disputes, quality concerns, customs/shipping issues with specific orders. Submit via ticketing system.'},
                {'q': 'What can an AI agent handle independently?', 'a': 'Answer product/lens questions, recommend frames and lenses, explain prescriptions, provide shipping/payment/policy info, guide through ordering, help with PD measurement.'},
            ]
        },
    ]

    all_faq_items = [item for section in faq_sections for item in section['questions']]
    return render_template('ai_faq.html', faq_sections=faq_sections, all_faq_items=all_faq_items, is_india=is_india, site_url=site_url)


@bp.route('/ai-knowledge-base.json')
def ai_knowledge_base():
    """Machine-readable Q&A knowledge base for AI agents."""
    return send_from_directory(os.path.join(current_app.root_path, 'static', 'ai'), 'optiwar_ai_knowledge_base.json', mimetype='application/json')


@bp.route('/ai-order-schema.json')
def ai_order_schema():
    """AI order schema with required/optional fields and decision rules."""
    return send_from_directory(os.path.join(current_app.root_path, 'static', 'ai'), 'ai-order-schema.json', mimetype='application/json')


@bp.route('/ai-lens-rules.json')
def ai_lens_rules():
    """Structured lens recommendation decision tree."""
    return send_from_directory(os.path.join(current_app.root_path, 'static', 'ai'), 'lens_rules.json', mimetype='application/json')


@bp.route('/ai-frame-rules.json')
def ai_frame_rules():
    """Face measurement to frame sizing rules."""
    return send_from_directory(os.path.join(current_app.root_path, 'static', 'ai'), 'frame_rules.json', mimetype='application/json')


@bp.route('/ai-prescription-rules.json')
def ai_prescription_rules():
    """Prescription field definitions and validation rules."""
    return send_from_directory(os.path.join(current_app.root_path, 'static', 'ai'), 'prescription_rules.json', mimetype='application/json')


@bp.route('/ai-guide/pd-measurement')
def guide_pd_measurement():
    """PD measurement guide page with HowTo and FAQPage JSON-LD."""
    site_url = 'https://optiwar.in' if _req_is_india() else 'https://optiwar.com'
    is_india = _req_is_india()
    return render_template('guide_pd_measurement.html', site_url=site_url, is_india=is_india)


@bp.route('/ai-guide/prescription-reading')
def guide_prescription_reading():
    """Prescription reading guide page with FAQPage JSON-LD."""
    site_url = 'https://optiwar.in' if _req_is_india() else 'https://optiwar.com'
    is_india = _req_is_india()
    return render_template('guide_prescription_reading.html', site_url=site_url, is_india=is_india)


@bp.route('/ai.txt')
def ai_txt():
    """AI crawler discovery file — like robots.txt but for AI agents."""
    site_url = 'https://optiwar.in' if _req_is_india() else 'https://optiwar.com'
    text = f"""# ai.txt — Optiwar AI Discovery
# This file helps AI agents and answer engines discover machine-readable resources.

User-agent: *
Allow: /

# Machine-readable knowledge
AI-Knowledge-Base: {site_url}/ai-knowledge-base.json
AI-Order-Schema: {site_url}/ai-order-schema.json
AI-Lens-Rules: {site_url}/ai-lens-rules.json
AI-Frame-Rules: {site_url}/ai-frame-rules.json
AI-Prescription-Rules: {site_url}/ai-prescription-rules.json
AI-FAQ: {site_url}/ai-faq
LLMs-txt: {site_url}/llms.txt
OpenAPI: {site_url}/api/openapi.json
AI-Plugin: {site_url}/.well-known/ai-plugin.json
UCP: {site_url}/.well-known/ucp

# Product API
Products-API: {site_url}/api/v1/products
Categories-API: {site_url}/api/v1/categories
Product-Detail-API: {site_url}/api/v1/products/{{id}}

# Human-readable guides
PD-Measurement-Guide: {site_url}/ai-guide/pd-measurement
Prescription-Reading-Guide: {site_url}/ai-guide/prescription-reading
Frame-Shapes-Guide: {site_url}/guides/frame-shapes

# Human-readable pages
Homepage: {site_url}/
All-Frames: {site_url}/eyeglasses/all-spectacle-frames.html
Lenses-Guide: {site_url}/lenses
Frame-Shapes: {site_url}/guides/frame-shapes
Face-Measurement: {site_url}/tryon
Terms: {site_url}/terms_and_conditions
Privacy: {site_url}/privacy_policy
Sitemap: {site_url}/sitemap.xml

# Business
Operator: Krishna Ecommerce Technologies Private Limited
Contact: support@optiwar.com
Response-Time: 24 hours
"""
    response = make_response(text)
    response.headers['Content-Type'] = 'text/plain; charset=utf-8'
    response.headers['Cache-Control'] = 'public, max-age=86400'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


# ===== FACE MEASUREMENT v4 =====
@bp.route('/tryon')
def spectacle_tryon():
    if 'user_id' not in session:
        return redirect(url_for('auth.login', next='/tryon'))
    return render_template("tryon.html")


@bp.route('/api/tryon/frames')
def api_tryon_frames():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT product_id, product_name, product_code, product_image, product_size
        FROM products
        WHERE product_category = 'Spectacles Frame'
        AND product_image IS NOT NULL AND product_image != ''
        AND product_quantity > 0
        ORDER BY RAND()
        LIMIT 12
    """)
    rows = cursor.fetchall()
    frames = []
    for row in rows:
        img = row['product_image'].split(',')[0].strip() if row['product_image'] else None
        if not img:
            continue
        frame_width_mm = 135
        if row.get('product_size'):
            parts = str(row['product_size']).split('-')
            if len(parts) >= 2:
                try:
                    lens_w = int(parts[0])
                    bridge = int(parts[1])
                    frame_width_mm = (lens_w * 2) + bridge + 10
                except (ValueError, IndexError):
                    pass
        frames.append({
            "id": row['product_id'],
            "name": f"{row['product_name']} {row['product_code']}",
            "image": f"/static/{img}",
            "frame_width_mm": frame_width_mm,
            "scale": 2.25
        })
    return jsonify(frames)


# ===== FACE MEASUREMENT v4 - SAVE & MATCH =====
import base64
import uuid
from datetime import datetime as dt_mod

@bp.route('/api/tryon/save', methods=['POST'])
def api_tryon_save():
    """Save face measurements + screenshot to customer profile"""
    if 'user_id' not in session:
        return jsonify({"error": "Login required"}), 401
    
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    
    customer_id = session['user_id']
    pd_far = data.get('pd_far')
    pd_near = data.get('pd_near')
    face_width = data.get('face_width')
    eye_mouth = data.get('eye_mouth')
    rec_diameter = data.get('recommended_diameter')
    rec_bridge = data.get('recommended_bridge')
    rec_length = data.get('recommended_length')
    decentration = data.get('decentration')
    frame_candidates_raw = data.get('frame_candidates', [])
    screenshot_b64 = data.get('screenshot')
    
    # Serialize frame candidates as JSON string
    import json as _json
    frame_candidates_json = _json.dumps(frame_candidates_raw) if frame_candidates_raw else None
    
    # Save screenshot image
    screenshot_path = None
    if screenshot_b64:
        try:
            # Remove data:image/png;base64, prefix
            if ',' in screenshot_b64:
                screenshot_b64 = screenshot_b64.split(',')[1]
            img_data = base64.b64decode(screenshot_b64)
            fname = f"face_{customer_id}_{uuid.uuid4().hex[:8]}.png"
            save_dir = os.path.join(current_app.root_path, 'static', 'tryon', 'captures')
            os.makedirs(save_dir, exist_ok=True)
            fpath = os.path.join(save_dir, fname)
            with open(fpath, 'wb') as f:
                f.write(img_data)
            screenshot_path = f"tryon/captures/{fname}"
        except Exception as e:
            print(f"Screenshot save error: {e}")
    
    db = get_db()
    cursor = db.cursor()
    
    # Delete old measurement for this customer (keep latest only)
    cursor.execute("DELETE FROM face_measurements WHERE customer_id = %s", (customer_id,))
    
    cursor.execute("""
        INSERT INTO face_measurements 
        (customer_id, pd_far, pd_near, face_width, eye_mouth, 
         recommended_diameter, recommended_bridge, recommended_length,
         decentration, frame_candidates, screenshot_path)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (customer_id, pd_far, pd_near, face_width, eye_mouth,
          rec_diameter, rec_bridge, rec_length,
          decentration, frame_candidates_json, screenshot_path))
    db.commit()
    
    return jsonify({
        "success": True,
        "message": "Measurements saved to your profile",
        "data": {
            "pd_far": pd_far,
            "pd_near": pd_near,
            "face_width": face_width,
            "recommended_size": f"{rec_diameter}-{rec_bridge}-{rec_length}"
        }
    })


@bp.route('/api/tryon/matching-frames')
def api_tryon_matching_frames():
    """Get frames matching customer face using proper optical formulas.
    
    Logic:
    1. frame_width = (lens×2) + bridge + 10
    2. Good frame width range = face_width ±5mm, Excellent = ±3mm
    3. Decentration per eye = (lens+bridge - PD) / 2
       ≤4mm = good, 4-6mm = acceptable, >6mm = avoid
    4. Temple from face width lookup table
    """
    if 'user_id' not in session:
        return jsonify({"error": "Login required"}), 401
    
    customer_id = session['user_id']
    db = get_db()
    cursor = db.cursor()
    
    cursor.execute("""
        SELECT pd_far, pd_near, face_width, recommended_diameter, recommended_bridge, recommended_length
        FROM face_measurements WHERE customer_id = %s
        ORDER BY measured_at DESC LIMIT 1
    """, (customer_id,))
    meas = cursor.fetchone()
    
    if not meas:
        return jsonify({"error": "No measurements found. Please scan your face first."}), 404
    
    pd_far = float(meas['pd_far']) if meas['pd_far'] else 63.0
    face_w = float(meas['face_width']) if meas['face_width'] else 132.0
    rec_d = meas['recommended_diameter'] or 52
    rec_b = meas['recommended_bridge'] or 18
    rec_l = meas['recommended_length'] or 140
    
    cursor.execute("""
        SELECT product_id, product_name, product_code, product_image, product_size, 
               product_price, product_color, product_shape
        FROM products
        WHERE product_category = 'Spectacles Frame'
        AND product_image IS NOT NULL AND product_image != ''
        AND product_quantity > 0
        AND product_size IS NOT NULL AND product_size != ''
        AND product_size REGEXP '^[0-9]+-[0-9]+-[0-9]+$'
    """)
    rows = cursor.fetchall()
    
    matching = []
    for row in rows:
        parts = str(row['product_size']).split('-')
        if len(parts) < 3:
            continue
        try:
            d = int(parts[0])
            b = int(parts[1])
            l = int(parts[2])
        except ValueError:
            continue
        
        # 1. Frame width check
        frame_total = (d * 2) + b + 10
        width_diff = abs(frame_total - face_w)
        if width_diff > 8:
            continue
        
        # 2. Decentration check
        frame_pcd = d + b
        decentration = abs(frame_pcd - pd_far) / 2.0
        if decentration > 6:
            continue
        
        # 3. Temple tolerance
        l_diff = abs(l - rec_l)
        if l_diff > 10:
            continue
        
        img = row['product_image'].split(',')[0].strip() if row['product_image'] else None
        if not img:
            continue
        
        # Score: lower is better
        score = width_diff * 1.5 + decentration * 2 + l_diff * 0.3
        
        # Fit classification
        if width_diff <= 3 and decentration <= 4:
            fit = "Perfect"
        elif width_diff <= 5 and decentration <= 5:
            fit = "Good"
        else:
            fit = "Fair"
        
        matching.append({
            "id": row['product_id'],
            "name": f"{row['product_name'] or ''} {row['product_code'] or ''}".strip(),
            "image": f"/static/{img}",
            "media": build_media_primary(row['product_image'] or ''),
            "size": row['product_size'],
            "price": float(row['product_price']) if row['product_price'] else 0,
            "color": row['product_color'] or '',
            "shape": row['product_shape'] or '',
            "score": round(score, 1),
            "fit": fit,
            "frame_width": frame_total,
            "decentration": round(decentration, 1)
        })
    
    matching.sort(key=lambda x: x['score'])
    
    return jsonify({
        "measurements": {
            "pd_far": pd_far,
            "pd_near": float(meas['pd_near']) if meas['pd_near'] else None,
            "face_width": face_w,
            "recommended_size": f"{rec_d}-{rec_b}-{rec_l}"
        },
        "frames": matching[:24],
        "total": len(matching),
        "media_schema": MEDIA_SCHEMA_VERSION
    })


@bp.route('/api/tryon/my-measurements')
def api_tryon_my_measurements():
    """Get customer's saved measurements"""
    if 'user_id' not in session:
        return jsonify({"error": "Login required"}), 401
    
    customer_id = session['user_id']
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT * FROM face_measurements WHERE customer_id = %s
        ORDER BY measured_at DESC LIMIT 1
    """, (customer_id,))
    meas = cursor.fetchone()
    
    if not meas:
        return jsonify({"has_measurements": False})
    
    return jsonify({
        "has_measurements": True,
        "pd_far": float(meas['pd_far']) if meas['pd_far'] else None,
        "pd_near": float(meas['pd_near']) if meas['pd_near'] else None,
        "face_width": float(meas['face_width']) if meas['face_width'] else None,
        "eye_mouth": float(meas['eye_mouth']) if meas['eye_mouth'] else None,
        "recommended_size": f"{meas['recommended_diameter']}-{meas['recommended_bridge']}-{meas['recommended_length']}",
        "screenshot": f"/static/{meas['screenshot_path']}" if meas['screenshot_path'] else None,
        "measured_at": meas['measured_at'].isoformat() if meas['measured_at'] else None
    })
# ===== END FACE MEASUREMENT v4 =====
