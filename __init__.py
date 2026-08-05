import os
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, request, session, make_response, redirect, jsonify
from flask_mysqldb import MySQL
import MySQLdb.cursors
from flask_mail import Mail, Message
from flask_compress import Compress
from flask_session import Session
from flask_babel import Babel, gettext as _
from .payments import initiate_payment
from .orders import bp as orders_bp
from .crm import bp as crm_bp
from .info import bp as info_bp
from .captcha import CaptchaGenerator
from .products import bp as products_bp
from .cl_range_model import bp as cl_range_model_bp
from .auth import bp as auth_bp, init_oauth
from .profile import bp as profile_bp
from .pricing import bp as pricing_bp
from .chat import bp as chat_bp, init_chat
from .chat_gateway import bp as chat_gateway_bp, init_chat_gateway
import logging
from logging.handlers import TimedRotatingFileHandler
from werkzeug.middleware.proxy_fix import ProxyFix
#from random import randint
#from .orders import my_orders

mail = Mail()
compress = Compress()
captcha_generator = CaptchaGenerator()

def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config['DEBUG'] = False
    app.config['ENV'] = 'prouction'
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', '#4418@1220042ksk$dkdk%sdskl!!')
    app.config['SESSION_TYPE'] = 'filesystem'
    # Session cookie hardening
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SECURE'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)  # absolute cap
    # P1 host allow-list (defense-in-depth; nginx default_server already rejects unknown hosts at the edge)
    app.config['TRUSTED_HOSTS'] = ['optiwar.com', 'www.optiwar.com', 'optiwar.in', 'www.optiwar.in', 'in.optiwar.com', 'localhost', '127.0.0.1']
    app.config['MYSQL_HOST'] = 'localhost'
    app.config['MYSQL_USER'] = 'oslb6'
    app.config['MYSQL_PASSWORD'] = os.environ.get('MYSQL_PASSWORD', '')
    app.config['MYSQL_DB'] = 'optiwar2'
    app.config['MAIL_DEBUG'] = False
    app.config['MAIL_SERVER'] = 'mail.ket.ltd'
    app.config['MAIL_PORT'] = 587
    app.config['MAIL_USE_TLS'] = True
    app.config['MAIL_USE_SSL'] = False
    app.config['MAIL_USERNAME'] = 'admin@optiwar.com'
    app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
    app.config['MAIL_DEFAULT_SENDER'] = 'admin@optiwar.com'
    app.config['PAYTM_MID'] = os.environ.get('PAYTM_MID', '')
    app.config['PAYTM_MERCHANT_KEY'] = os.environ.get('PAYTM_MERCHANT_KEY', '')

    # Razorpay (optiwar.com global EUR payments)
    app.config['RAZORPAY_KEY_ID'] = os.environ.get('RAZORPAY_KEY_ID', '')
    app.config['RAZORPAY_KEY_SECRET'] = os.environ.get('RAZORPAY_KEY_SECRET', '')

    # MSG91 (WhatsApp & SMS notifications)
    app.config['MSG91_AUTH_KEY'] = os.environ.get('MSG91_AUTH_KEY', '')
    app.config['MSG91_WHATSAPP_NUMBER'] = '919355380318'
    app.config['MSG91_SMS_SENDER'] = 'OPTWAR'
    # Optional shared token to authenticate MSG91 delivery-status callbacks.
    app.config['MSG91_DELIVERY_TOKEN'] = os.environ.get('MSG91_DELIVERY_TOKEN', '')

    # KET comms-hub event emission (payment/order events -> support.ket.ltd/external/events)
    # KET is email-ticketing only; Optiwar owns WhatsApp directly (MSG91) and SMS is off,
    # so the event channel allow-list is empty by default to prevent any duplicate delivery.
    app.config['KET_EVENTS_ENABLED'] = os.environ.get('KET_EVENTS_ENABLED', 'true').lower() == 'true'
    app.config['KET_EVENT_CHANNELS'] = os.environ.get('KET_EVENT_CHANNELS', '')
    # Support-ticket customer email is owned by KET (sole sender); Optiwar sends only WhatsApp for support
    app.config['SUPPORT_TICKET_EMAIL_ENABLED'] = os.environ.get('SUPPORT_TICKET_EMAIL_ENABLED', 'false').lower() == 'true'

    # ticket_created WhatsApp stays OFF until the resolved/reopened lifecycle
    # webhook passes joint acceptance (per KET direction).
    app.config['TICKET_CREATED_WHATSAPP_ENABLED'] = os.environ.get('TICKET_CREATED_WHATSAPP_ENABLED', 'false').lower() == 'true'

    # Admin address for internal-only notices (e.g. payment_attempted)
    app.config['ADMIN_NOTIFY_EMAIL'] = os.environ.get('ADMIN_NOTIFY_EMAIL', 'admin@optiwar.com')

    # App-side ticket admin-notification email (default OFF: KET is now sole sender)
    app.config['APP_TICKET_EMAIL_ENABLED'] = os.environ.get('APP_TICKET_EMAIL_ENABLED', 'false').lower() == 'true'

    # DeepSeek API (AI Chat Search)
    app.config['DEEPSEEK_API_KEY'] = os.environ.get('DEEPSEEK_API_KEY', '')
    # OpenAI API (GPT-4o Vision for prescription parsing)
    app.config['OPENAI_API_KEY'] = os.environ.get('OPENAI_API_KEY', '')

    # KET Support inbound webhooks (agent-reply/resolve) are HMAC-signed with this
    # shared secret. Server-side only -- no source fallback. Absent secret => the
    # webhook verifier fails closed (rejects all KET calls).
    app.config['OPTIWAR_WEBHOOK_SECRET'] = os.environ.get('OPTIWAR_WEBHOOK_SECRET', '')

    # ═══ TEST_PAY: Set to False to disable the test payment button on checkout ═══
    # To disable: change True to False below and restart gunicorn
    #   sudo systemctl restart gunicorn
    app.config['TEST_PAY_ENABLED'] = False
    #app.config['PAYTM_CALLBACK_URL'] = url_for('main.payment_callback', _external=True)
    app.config_generator = captcha_generator

    # Google OAuth configuration
    app.config['GOOGLE_OAUTH_CLIENT_ID'] = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
    app.config['GOOGLE_OAUTH_CLIENT_SECRET'] = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')

    # Google Analytics 4 (gtag.js). Set GA_MEASUREMENT_ID (G-XXXXXXXXXX) to enable
    # the tag site-wide; leave empty to disable. Optional GA_MEASUREMENT_ID_IN lets
    # optiwar.in use a separate GA4 data stream (falls back to GA_MEASUREMENT_ID).
    app.config['GA_MEASUREMENT_ID'] = os.environ.get('GA_MEASUREMENT_ID', '')
    app.config['GA_MEASUREMENT_ID_IN'] = os.environ.get('GA_MEASUREMENT_ID_IN', '')

    mail.init_app(app)
    Session(app)

    # Session store: Redis (db=1) with zero-logout lazy fallback to the legacy
    # filesystem records. On a Redis miss the old filesystem session is read once
    # (same store key) and persisted to Redis on the next save, so migrating
    # filesystem->Redis logs nobody out. If Redis is unavailable at boot, the
    # filesystem interface configured above stays in effect.
    try:
        import redis as _redis_sess
        from .redis_session_migrate import DualReadRedisSessionInterface
        _sess_client = _redis_sess.Redis(host='127.0.0.1', port=6379, db=1)
        _sess_client.ping()
        from datetime import datetime as _dt, timezone as _tz
        # dual-read retirement: one max session lifetime past the 2026-07-16 cutover.
        _sess_retire_at = _dt(2026, 7, 23, tzinfo=_tz.utc).timestamp()
        app.session_interface = DualReadRedisSessionInterface(
            app,
            client=_sess_client,
            legacy_dir=os.path.join(os.getcwd(), 'flask_session'),
            retire_at_epoch=_sess_retire_at,
        )
        app.logger.info('Session store: Redis db=1 (legacy filesystem fallback active; retires 2026-07-23)')
    except Exception as _sess_err:
        app.logger.error(f'Session Redis init failed; staying on filesystem: {_sess_err}')

    # Idle timeout: log out authenticated sessions inactive > 24h
    _IDLE_TIMEOUT = 24 * 3600
    @app.before_request
    def _enforce_idle_timeout():
        if session.get('user_id'):
            now = time.time()
            last = session.get('_last_seen')
            if last and (now - last) > _IDLE_TIMEOUT:
                session.clear()
            else:
                session['_last_seen'] = now

    # Initialize Google OAuth
    init_oauth(app)

    # ═══ MULTI-LANGUAGE: Flask-Babel ═══
    SUPPORTED_LANGUAGES = {
        # Global (optiwar.com)
        'en': 'English',
        'de': 'Deutsch',
        'fr': 'Français',
        'ar': 'العربية',
        'es': 'Español',
        'ja': '日本語',
        # Indian (in.optiwar.com)
        'hi': 'हिन्दी',
        'bn': 'বাংলা',
        'te': 'తెలుగు',
        'mr': 'मराठी',
        'ta': 'தமிழ்',
        'gu': 'ગુજરાતી',
        'kn': 'ಕನ್ನಡ',
        'ml': 'മലയാളം',
        'pa': 'ਪੰਜਾਬੀ',
    }
    GLOBAL_LANGUAGES = ['en', 'de', 'fr', 'ar', 'es', 'ja']
    INDIA_LANGUAGES = ['en', 'hi', 'bn', 'te', 'mr', 'ta', 'gu', 'kn', 'ml', 'pa']
    RTL_LANGUAGES = ['ar']

    app.config['BABEL_DEFAULT_LOCALE'] = 'en'
    app.config['BABEL_TRANSLATION_DIRECTORIES'] = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'translations'
    )

    def get_locale():
        lang = request.cookies.get('lang')
        if lang and lang in SUPPORTED_LANGUAGES:
            return lang
        return request.accept_languages.best_match(
            list(SUPPORTED_LANGUAGES.keys()), default='en'
        )

    babel = Babel(app, locale_selector=get_locale)

    @app.route('/set-language', methods=['POST'])
    def set_language():
        lang = request.form.get('lang', 'en')
        if lang not in SUPPORTED_LANGUAGES:
            lang = 'en'
        referrer = request.form.get('next') or request.referrer or '/'
        resp = make_response(redirect(referrer))
        resp.set_cookie('lang', lang, max_age=365*24*3600, httponly=False,
                        samesite='Lax', secure=True)
        return resp

    @app.context_processor
    def inject_i18n():
        is_india = False
        try:
            host = request.host.lower()
            if 'in.optiwar.com' in host or 'optiwar.in' in host:
                is_india = True
        except:
            pass
        current_lang = get_locale()
        available = INDIA_LANGUAGES if is_india else GLOBAL_LANGUAGES
        return {
            'current_lang': current_lang,
            'available_languages': {k: SUPPORTED_LANGUAGES[k] for k in available},
            'is_rtl': current_lang in RTL_LANGUAGES,
            'all_languages': SUPPORTED_LANGUAGES,
        }

    # 🔐 Ensure logs directory exists
    log_dir = '/var/log/optiwar'
    debug_log_path = os.path.join(log_dir, 'debug.log')
    os.makedirs(log_dir, exist_ok=True)

    # 🧾 Configure daily rotating log handler
    file_handler = TimedRotatingFileHandler(
        debug_log_path, when='midnight', interval=1, backupCount=30, utc=True
    )
    file_handler.suffix = '%Y-%m-%d'
    file_handler.setFormatter(logging.Formatter(
        '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
    ))
    file_handler.setLevel(logging.DEBUG)

    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.DEBUG)
    app.logger.propagate = False

    # 🛡️ Log IP, UA, session info before every request
    @app.before_request
    def log_request_info():
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        uid = session.get('user_id', None)
        skeys = list(session.keys())
        app.logger.debug(f"[{request.host}] IP:{ip} Path:{request.path} user_id={uid} keys={skeys}")

    # Make user session data available to all templates
    # Helper to determine site origin from request host
    def get_site_from():
        """Returns 'in.optiwar.com' or 'optiwar.com' based on request host."""
        try:
            host = request.host.lower()
            if 'in.optiwar.com' in host or 'optiwar.in' in host:
                return 'in.optiwar.com'
        except:
            pass
        return 'optiwar.com'

    @app.context_processor
    def inject_user():
        is_india = get_site_from() == 'in.optiwar.com'
        _site_url = 'https://optiwar.in' if is_india else 'https://optiwar.com'
        return {
            'logged_in': 'user_id' in session,
            'current_user_name': session.get('user_name', ''),
            'current_user_email': session.get('user_email', ''),
            'current_user_phone': session.get('user_phone', ''),
            'current_user_id': session.get('user_id'),
            'cart_count': len(session.get('cart', [])),
            'is_india': is_india,
            'site_from': get_site_from(),
            'currency_symbol': '\u20b9' if is_india else '\u20ac',
            'site_url': _site_url,
            'test_pay_enabled': app.config.get('TEST_PAY_ENABLED', False) and datetime.now(timezone(timedelta(hours=5, minutes=30))).hour < 17,
        }

    #@app.after_request
    #def add_header(response):
        # For every response
    #    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    #    response.headers['Pragma'] = 'no-cache'
    #    response.headers['Expires'] = '0'
    #    return response

    @app.after_request
    def set_login_indicator_cookie(response):
        """Set ow_uid cookie for logged-in users so nginx can bypass cache."""
        if session.get('user_id'):
            response.set_cookie('ow_uid', '1', httponly=False, samesite='Lax', secure=True, max_age=86400*30)
        else:
            if request.cookies.get('ow_uid'):
                response.delete_cookie('ow_uid')
        return response

    #@app.context_processor
    #def inject_flush():
    #    return {'randon': lambda: randint(10000, 99999)}


    if test_config is None:
        app.config.from_pyfile('config.py', silent=True)
    else: 
        app.config.from_mapping(test_config)

    try:
        os.makedirs(app.instance_path)
    except OSError:
       pass

    from .import db
    db.init_app(app)

    from . import compress
    compress.init_app(app)

    from .import models
    app.register_blueprint(models.bp) 

    #from .import crm
    app.register_blueprint(crm_bp)
    app.register_blueprint(info_bp)
    #from .import orders
    #app.register_blueprint(orders.bp)
    app.register_blueprint(orders_bp)
    app.register_blueprint(products_bp)

    from .orders import bp
    from .products import bp
    from . import payments
    from .cl_range_model import bp
    app.register_blueprint(cl_range_model_bp)

    # Register auth blueprint
    app.register_blueprint(auth_bp)

    # Register profile blueprint
    app.register_blueprint(profile_bp)

    # Register pricing blueprint
    app.register_blueprint(pricing_bp)

    # Register AI chat blueprint
    app.register_blueprint(chat_bp)
    init_chat(app)


    from . import ops
    app.register_blueprint(ops.bp)

    from . import ai_api
    app.register_blueprint(ai_api.bp)
    # Register custom chat gateway blueprint
    app.register_blueprint(chat_gateway_bp)
    init_chat_gateway(app)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0, x_port=0, x_prefix=0)

    # CSRF Phase 1b: enforcement toggle (env-driven; default off = log-only)
    app.config['CSRF_ENFORCE'] = os.environ.get('CSRF_ENFORCE', 'false').lower() == 'true'
    # CSRF Phase 1: Origin/Referer verification (log-only until CSRF_ENFORCE=True)
    from .csrf_guard import init_csrf_guard
    init_csrf_guard(app)

    # AI capacity/deadline wrapper: validate thinking config + log wrapper state
    from .ai_client import init_ai_client
    init_ai_client(app)

    # Responsive image embed: load derivative manifest once, expose Jinja helpers
    from .embed_helper import register_image_helpers
    register_image_helpers(app)

    # Restart-safe KET WhatsApp outbox: resumes pending/failed delivery jobs so a
    # gunicorn restart or crash after a webhook 200 never loses the notification.
    from .crm import start_whatsapp_outbox_worker
    start_whatsapp_outbox_worker(app)

    @app.route('/hello')
    def hello():
        return ' Hello everyone - Optiwar2 is back'

    return app