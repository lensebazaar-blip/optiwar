from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import generate_password_hash, check_password_hash
from .captcha import CaptchaGenerator
from .db import get_db
from .cart_persist import load_cart_from_db, save_cart_to_db
from .test_identity import is_test_customer
from authlib.integrations.flask_client import OAuth
from authlib.integrations.base_client.errors import MismatchingStateError
import functools
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse


def _is_safe_redirect(target):
    """Validate redirect target to prevent open redirect attacks."""
    if not target:
        return False
    parsed = urlparse(target)
    # Only allow relative paths or same-host URLs
    return parsed.netloc == '' or parsed.netloc in (
        'optiwar.com', 'in.optiwar.com', 'www.optiwar.com',
        'optiwar.in', 'www.optiwar.in'
    )

bp = Blueprint('auth', __name__, url_prefix='/auth')
captcha_generator = CaptchaGenerator()
oauth = OAuth()


def init_oauth(app):
    oauth.init_app(app)
    oauth.register(
        name='google',
        client_id=app.config.get('GOOGLE_OAUTH_CLIENT_ID'),
        client_secret=app.config.get('GOOGLE_OAUTH_CLIENT_SECRET'),
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_kwargs={'scope': 'openid email profile'},
    )


def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.')
            return redirect(url_for('auth.login', next=request.url))
        return view(**kwargs)
    return wrapped_view


def is_valid_email(email):
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def _complete_login(cursor, user, must_set_password=False, google_id=None):
    current_app.session_interface.regenerate(session)
    session.pop('must_set_password', None)
    session['user_id'] = user['customer_id']
    session['user_name'] = user['customer_name']
    session['user_email'] = user['customer_email']
    session['user_phone'] = user.get('customer_phone', '')
    session['is_test_customer'] = is_test_customer(cursor, user['customer_id'])
    if google_id:
        session['google_id'] = google_id
    if must_set_password:
        session['must_set_password'] = True
    load_cart_from_db()
    if session.get('cart'):
        save_cart_to_db()


def _temporary_password_expired(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value <= datetime.now()


@bp.before_app_request
def _require_temporary_password_change():
    if not session.get('user_id') or not session.get('must_set_password'):
        return None
    if request.endpoint in ('main.set_password', 'auth.set_password',
                            'auth.logout', 'static'):
        return None
    if request.path.startswith('/static/'):
        return None
    is_json_or_xhr = (
        request.is_json
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.accept_mimetypes.best == 'application/json'
    )
    if is_json_or_xhr:
        return jsonify({"error": "password_change_required"}), 403
    return redirect('/set-password')


@bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        phone = request.form.get('phone', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        user_captcha = request.form.get('captcha', '').strip()

        # Validate CAPTCHA
        if user_captcha.upper() != session.get('captcha', '').upper():
            flash('Invalid CAPTCHA. Please try again.', 'danger')
            return redirect(url_for('auth.register'))

        # Validate fields
        if not name or not email or not password:
            flash('Name, email and password are required.', 'danger')
            return redirect(url_for('auth.register'))

        if not is_valid_email(email):
            flash('Please enter a valid email address.', 'danger')
            return redirect(url_for('auth.register'))

        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return redirect(url_for('auth.register'))

        if password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('auth.register'))

        db = get_db()
        cursor = db.cursor()

        # Check if email already exists
        cursor.execute('SELECT customer_id FROM customers WHERE customer_email = %s', (email,))
        existing = cursor.fetchone()
        if existing:
            flash('An account with this email already exists. Please log in.', 'danger')
            return redirect(url_for('auth.login'))

        try:
            hashed_password = generate_password_hash(password)
            cursor.execute(
                'INSERT INTO customers (customer_name, customer_email, customer_password, customer_phone) VALUES (%s, %s, %s, %s)',
                (name, email, hashed_password, phone)
            )
            db.commit()
            customer_id = cursor.lastrowid

            # Log user in immediately after registration
            current_app.session_interface.regenerate(session)  # prevent session fixation
            session['user_id'] = customer_id
            session['user_name'] = name
            session['user_email'] = email
            session['user_phone'] = phone

            current_app.logger.info(f'[{request.host}] ACTIVITY:REGISTER IP:{request.headers.get("X-Forwarded-For", request.remote_addr)} user:{customer_id} email:{email}')
            flash('Account created successfully! You are now logged in.', 'success')

            # Redirect to intended page (cart, tryon, etc.) or homepage
            next_url = request.args.get('next') or request.form.get('next') or session.pop('_login_next', None)
            if next_url and _is_safe_redirect(next_url):
                return redirect(next_url)
            return redirect(url_for('main.index'))

        except Exception as e:
            db.rollback()
            current_app.logger.error(f'Registration error: {e}', exc_info=True)
            flash('An error occurred during registration. Please try again.', 'danger')
            return redirect(url_for('auth.register'))

    # GET request - generate captcha
    captcha_text = captcha_generator.generate_captcha()
    session['captcha'] = captcha_text
    captcha_image = captcha_generator.generate_captcha_image(captcha_text)

    return render_template('register.html', captcha_image=captcha_image)


@bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user_captcha = request.form.get('captcha', '').strip()

        # Validate CAPTCHA
        if user_captcha.upper() != session.get('captcha', '').upper():
            flash('Invalid CAPTCHA. Please try again.', 'danger')
            return redirect(url_for('auth.login'))

        if not email or not password:
            flash('Email and password are required.', 'danger')
            return redirect(url_for('auth.login'))

        db = get_db()
        cursor = db.cursor()

        cursor.execute(
            'SELECT customer_id, customer_name, customer_email, customer_password, customer_phone FROM customers WHERE customer_email = %s',
            (email,)
        )
        user = cursor.fetchone()

        if user is None:
            flash('No account found with this email. Please register first.', 'danger')
            return redirect(url_for('auth.login'))

        if not user['customer_password']:
            flash('This account uses Google login. Please sign in with Google.', 'danger')
            return redirect(url_for('auth.login'))

        if not check_password_hash(user['customer_password'], password):
            flash('Incorrect password. Please try again.', 'danger')
            current_app.logger.warning(f'[{request.host}] ACTIVITY:LOGIN_FAILED IP:{request.headers.get("X-Forwarded-For", request.remote_addr)} email:{email}')
            return redirect(url_for('auth.login'))

        from . import ops_customers
        ops_customers.ensure_schema(db)
        cursor.execute("SELECT expires_at FROM customer_temp_passwords "
                       "WHERE customer_id=%s", (user['customer_id'],))
        temporary_password = cursor.fetchone()
        if temporary_password and _temporary_password_expired(
                temporary_password['expires_at']):
            flash("Your temporary password has expired. Log in with a WhatsApp code or use Forgot password.",
                  'danger')
            return redirect(url_for('auth.login'))

        _complete_login(cursor, user, must_set_password=bool(temporary_password))

        current_app.logger.info(f'[{request.host}] ACTIVITY:LOGIN IP:{request.headers.get("X-Forwarded-For", request.remote_addr)} user:{session.get("user_id")} email:{email}')
        if temporary_password:
            next_url = (request.args.get('next') or request.form.get('next')
                        or session.get('_login_next'))
            if next_url and _is_safe_redirect(next_url):
                session['_login_next'] = next_url
            return redirect(url_for('main.set_password'))
        flash(f'Welcome back, {user["customer_name"]}!', 'success')

        # Redirect to intended page (cart, tryon, etc.) or homepage
        next_url = request.args.get('next') or request.form.get('next') or session.pop('_login_next', None)
        if next_url and _is_safe_redirect(next_url):
            return redirect(next_url)
        return redirect(url_for('main.index'))

    # GET request - generate captcha
    captcha_text = captcha_generator.generate_captcha()
    session['captcha'] = captcha_text
    captcha_image = captcha_generator.generate_captcha_image(captcha_text)

    # Preserve next URL for post-login redirect
    next_url = request.args.get('next', '') or session.get('_login_next', '')
    if next_url and _is_safe_redirect(next_url):
        session['_login_next'] = next_url
    else:
        next_url = ''

    return render_template(
        'login.html', captcha_image=captcha_image, next_url=next_url,
        otp_enabled=bool(current_app.config.get("MSG91_LOGIN_OTP_WA_TEMPLATE")),
        otp_pending=bool(session.get("_wa_login_otp_id")))


def set_password():
    if not session.get('user_id'):
        return redirect(url_for('auth.login'))
    if not session.get('must_set_password'):
        return redirect(url_for('main.index'))
    from . import ops_customers
    db = get_db()
    ops_customers.ensure_schema(db)
    cursor = db.cursor()
    cursor.execute("SELECT customer_id FROM customer_temp_passwords "
                   "WHERE customer_id=%s", (session['user_id'],))
    if not cursor.fetchone():
        session.pop('must_set_password', None)
        return redirect(url_for('main.index'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirmation = request.form.get('confirm_password', '')
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return render_template('set_password.html')
        if password != confirmation:
            flash('Passwords do not match.', 'danger')
            return render_template('set_password.html')

        cursor.execute("UPDATE customers SET customer_password=%s "
                       "WHERE customer_id=%s",
                       (generate_password_hash(password), session['user_id']))
        cursor.execute("DELETE FROM customer_temp_passwords WHERE customer_id=%s",
                       (session['user_id'],))
        db.commit()
        session.pop('must_set_password', None)
        flash('Your password has been updated.', 'success')
        next_url = session.pop('_login_next', None)
        if next_url and _is_safe_redirect(next_url):
            return redirect(next_url)
        return redirect(url_for('main.index'))
    return render_template('set_password.html')


def _otp_enabled():
    return bool(current_app.config.get("MSG91_LOGIN_OTP_WA_TEMPLATE"))


def _otp_hash(code):
    secret = str(current_app.config.get("SECRET_KEY") or "").encode("utf-8")
    return hmac.new(secret, str(code).encode("utf-8"), hashlib.sha256).hexdigest()


def _otp_redirect(message, category="info"):
    flash(message, category)
    return redirect(url_for('auth.login'))


def _otp_request_generic_reply(ops_customers):
    session["_wa_login_otp_id"] = 0
    session.pop("_wa_login_otp_sentinel_attempts", None)
    return _otp_redirect(ops_customers.OTP_GENERIC_MESSAGE, "info")


def request_whatsapp_otp():
    if not _otp_enabled():
        abort(404)
    from . import ops_customers

    data = request.get_json(silent=True) or request.form
    captcha = str(data.get("captcha") or "").strip()
    expected_captcha = session.pop("captcha", None)
    expected_captcha = str(expected_captcha or "").strip()
    if not expected_captcha or captcha.upper() != expected_captcha.upper():
        return _otp_redirect('Invalid CAPTCHA. Please try again.', 'danger')
    try:
        requested_phone = ops_customers.normalize_phone(data.get("phone"))
    except ops_customers.CustomerError:
        return _otp_request_generic_reply(ops_customers)

    db = get_db()
    ops_customers.ensure_schema(db)
    matches = ops_customers.find_customers(db, phone=requested_phone)
    if len(matches) != 1:
        return _otp_request_generic_reply(ops_customers)
    user = matches[0]
    try:
        stored_phone = ops_customers.normalize_phone(user["phone"])
    except ops_customers.CustomerError:
        return _otp_request_generic_reply(ops_customers)

    cursor = db.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS n FROM customer_login_otps "
        "WHERE phone=%s AND created_at >= NOW() - INTERVAL 15 MINUTE",
        (stored_phone,))
    phone_count = int((cursor.fetchone() or {}).get("n", 0))
    ip = request.remote_addr or ""
    cursor.execute(
        "SELECT COUNT(*) AS n FROM customer_login_otps "
        "WHERE created_ip=%s AND created_at >= NOW() - INTERVAL 15 MINUTE",
        (ip,))
    ip_count = int((cursor.fetchone() or {}).get("n", 0))
    if phone_count >= 3 or ip_count >= 3:
        return _otp_request_generic_reply(ops_customers)

    code = "%06d" % secrets.randbelow(1_000_000)
    expires_at = datetime.now() + timedelta(minutes=10)
    cursor.execute(
        "INSERT INTO customer_login_otps "
        "(customer_id, phone, code_hash, expires_at, attempts, created_ip) "
        "VALUES (%s,%s,%s,%s,0,%s)",
        (user["customer_id"], stored_phone, _otp_hash(code), expires_at, ip))
    otp_id = cursor.lastrowid
    try:
        from .notifications import send_whatsapp_tracked
        result = send_whatsapp_tracked(
            stored_phone.lstrip("+"),
            current_app.config["MSG91_LOGIN_OTP_WA_TEMPLATE"],
            {
                "body_1": {"type": "text", "value": code},
                "button_1": {
                    "subtype": "url", "type": "text", "value": code,
                },
            })
    except Exception:
        result = {"ok": False}
    if not result or not result.get("ok"):
        cursor.execute("UPDATE customer_login_otps SET used_at=NOW() WHERE id=%s",
                       (otp_id,))
        db.commit()
        return _otp_request_generic_reply(ops_customers)

    db.commit()
    session["_wa_login_otp_id"] = otp_id
    session.pop("_wa_login_otp_sentinel_attempts", None)
    return _otp_redirect(ops_customers.OTP_GENERIC_MESSAGE, "info")


def restart_whatsapp_otp():
    if not _otp_enabled():
        abort(404)
    session.pop("_wa_login_otp_id", None)
    session.pop("_wa_login_otp_sentinel_attempts", None)
    return redirect(url_for("auth.login"))


def verify_whatsapp_otp():
    if not _otp_enabled():
        abort(404)
    from . import ops_customers

    otp_id = session.get("_wa_login_otp_id")
    if otp_id == 0:
        attempts = int(session.get("_wa_login_otp_sentinel_attempts") or 0) + 1
        if attempts >= 5:
            session.pop("_wa_login_otp_id", None)
            session.pop("_wa_login_otp_sentinel_attempts", None)
        else:
            session["_wa_login_otp_sentinel_attempts"] = attempts
        return _otp_redirect("That code is incorrect or expired.", "danger")
    if not otp_id:
        return _otp_redirect("That code is incorrect or expired.", "danger")
    data = request.get_json(silent=True) or request.form
    code = str(data.get("code") or "").strip()
    db = get_db()
    ops_customers.ensure_schema(db)
    cursor = db.cursor()
    cursor.execute("SELECT * FROM customer_login_otps WHERE id=%s FOR UPDATE",
                   (otp_id,))
    otp = cursor.fetchone()
    if not otp:
        db.commit()
        session.pop("_wa_login_otp_id", None)
        return _otp_redirect("That code is incorrect or expired.", "danger")
    if (otp.get("used_at")
            or _temporary_password_expired(otp["expires_at"])):
        db.commit()
        session.pop("_wa_login_otp_id", None)
        return _otp_redirect("This code has expired. Request a new one.", "danger")

    if not re.fullmatch(r"\d{6}", code) or not hmac.compare_digest(
            otp["code_hash"], _otp_hash(code)):
        attempts = int(otp.get("attempts") or 0) + 1
        cursor.execute(
            "UPDATE customer_login_otps SET attempts=%s, "
            "used_at=IF(%s >= 5, NOW(), used_at) WHERE id=%s AND used_at IS NULL",
            (attempts, attempts, otp_id))
        db.commit()
        if attempts >= 5:
            session.pop("_wa_login_otp_id", None)
        return _otp_redirect("That code is incorrect or expired.", "danger")

    cursor.execute("UPDATE customer_login_otps SET used_at=NOW() "
                   "WHERE id=%s AND used_at IS NULL", (otp_id,))
    if cursor.rowcount != 1:
        db.commit()
        session.pop("_wa_login_otp_id", None)
        return _otp_redirect("This code has expired. Request a new one.", "danger")
    db.commit()
    session.pop("_wa_login_otp_id", None)
    cursor.execute(
        "SELECT customer_id, customer_name, customer_email, customer_phone "
        "FROM customers WHERE customer_id=%s", (otp["customer_id"],))
    user = cursor.fetchone()
    if not user:
        return _otp_redirect("This code has expired. Request a new one.", "danger")
    try:
        current_phone = ops_customers.normalize_phone(user.get("customer_phone"))
    except ops_customers.CustomerError:
        current_phone = None
    if current_phone != otp.get("phone"):
        return _otp_redirect("This code has expired. Request a new one.", "danger")
    _complete_login(cursor, user)
    current_app.logger.info(
        "[%s] ACTIVITY:LOGIN_WA_OTP user:%s", request.host, user["customer_id"])
    flash("Welcome back, %s!" % user["customer_name"], "success")
    next_url = session.pop('_login_next', None)
    if next_url and _is_safe_redirect(next_url):
        return redirect(next_url)
    return redirect(url_for('main.index'))


@bp.route('/logout')
def logout():
    user_name = session.get('user_name', 'User')
    current_app.session_interface.regenerate(session)  # rotate SID + delete backing record so a captured pre-logout cookie is dead
    session.clear()
    flash(f'You have been logged out, {user_name}.', 'success')
    return redirect(url_for('main.index'))


@bp.route('/google/login')
def google_login():
    # Store next URL before redirecting to Google OAuth
    next_url = request.args.get('next') or session.get('_login_next', '')
    if next_url and _is_safe_redirect(next_url):
        session['_login_next'] = next_url
    redirect_uri = url_for('auth.google_callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route('/google/callback')
def google_callback():
    try:
        token = oauth.google.authorize_access_token()
        user_info = token.get('userinfo')

        if not user_info:
            flash('Failed to get user information from Google.', 'danger')
            return redirect(url_for('auth.login'))

        google_id = user_info.get('sub')
        email = user_info.get('email', '').lower()
        name = user_info.get('name', '')

        if not email:
            flash('Could not retrieve email from Google account.', 'danger')
            return redirect(url_for('auth.login'))

        db = get_db()
        cursor = db.cursor()

        # Check if user exists by google_id first, then by email
        cursor.execute(
            'SELECT customer_id, customer_name, customer_email FROM customers WHERE google_id = %s',
            (google_id,)
        )
        user = cursor.fetchone()

        if user:
            # Found by google_id — if DB email differs from Google email, update it
            if user['customer_email'] != email:
                cursor.execute(
                    'UPDATE customers SET customer_email = %s WHERE customer_id = %s',
                    (email, user['customer_id'])
                )
                db.commit()
                current_app.logger.info(
                    f'Updated stale email for google user {user["customer_id"]}: '
                    f'{user["customer_email"]} -> {email}'
                )
                user = dict(user)
                user['customer_email'] = email
        else:
            # Check by email
            cursor.execute(
                'SELECT customer_id, customer_name, customer_email, customer_phone, google_id FROM customers WHERE customer_email = %s',
                (email,)
            )
            user = cursor.fetchone()

            if user:
                # Link Google account to existing user
                cursor.execute(
                    'UPDATE customers SET google_id = %s WHERE customer_id = %s',
                    (google_id, user['customer_id'])
                )
                db.commit()
                current_app.logger.info(f'Linked Google account to existing user: {email}')
            else:
                # Create new user
                cursor.execute(
                    'INSERT INTO customers (customer_name, customer_email, google_id) VALUES (%s, %s, %s)',
                    (name, email, google_id)
                )
                db.commit()
                customer_id = cursor.lastrowid
                user = {
                    'customer_id': customer_id,
                    'customer_name': name,
                    'customer_email': email
                }
                current_app.logger.info(f'New Google user created: {email} (ID: {customer_id})')

        user = dict(user)
        user['customer_email'] = email
        _complete_login(cursor, user, google_id=google_id)

        flash(f'Welcome, {user["customer_name"]}!', 'success')

        # Redirect to intended page (cart, tryon, etc.) or homepage
        next_url = session.pop('_login_next', None)
        if next_url and _is_safe_redirect(next_url):
            return redirect(next_url)
        return redirect(url_for('main.index'))

    except MismatchingStateError:
        user_id = session.get('user_id')
        if user_id:
            current_app.logger.info(
                "Google callback repeated after sign-in (stale state); user %s", user_id
            )
            next_url = session.pop('_login_next', None)
            if next_url and _is_safe_redirect(next_url):
                return redirect(next_url)
            return redirect(url_for('main.index'))
        current_app.logger.warning(
            "Google sign-in state mismatch (expired or repeated callback)"
        )
        flash('Your Google sign-in took too long or was opened twice. Please try again.',
              'warning')
        return redirect(url_for('auth.login'))
    except Exception as e:
        current_app.logger.error(f'Google OAuth error: {e}', exc_info=True)
        flash('An error occurred during Google login. Please try again.', 'danger')
        return redirect(url_for('auth.login'))


# ─── FORGOT PASSWORD (OTP via Email, SMS/WhatsApp ready) ─────────────

import random
import time

def _generate_otp():
    """Generate a 4-digit OTP."""
    return str(random.randint(1000, 9999))


def _send_reset_otp(email, otp, phone=None):
    """Send OTP via available channels (Email now, SMS/WhatsApp later).
    EWS-ready: when MSG91 is configured, SMS and WhatsApp will auto-activate.
    """
    results = {'email': False, 'sms': False, 'whatsapp': False}

    # Channel 1: Email (active now)
    try:
        from .mail import send_otp_email
        send_otp_email(email, otp)
        results['email'] = True
        current_app.logger.info(f'[{request.host}] RESET_OTP:EMAIL_SENT to={email}')
    except Exception as e:
        current_app.logger.error(f'[{request.host}] RESET_OTP:EMAIL_FAILED to={email} error={e}')

    # Channel 2: SMS (activate when MSG91 is configured)
    if phone and current_app.config.get('MSG91_AUTH_KEY'):
        try:
            from .notifications import send_sms
            flow_id = current_app.config.get('MSG91_RESET_OTP_FLOW_ID', '')
            if flow_id:
                send_sms(phone, flow_id, variables={'otp': otp})
                results['sms'] = True
        except Exception as e:
            current_app.logger.error(f'RESET_OTP:SMS_FAILED to={phone} error={e}')

    # Channel 3: WhatsApp (activate when MSG91 WhatsApp is configured)
    if phone and current_app.config.get('MSG91_AUTH_KEY') and current_app.config.get('MSG91_WHATSAPP_NUMBER'):
        try:
            from .notifications import send_whatsapp
            template = current_app.config.get('MSG91_RESET_OTP_WA_TEMPLATE', '')
            if template:
                send_whatsapp(phone, template, components={'body': [{'type': 'text', 'text': otp}]})
                results['whatsapp'] = True
        except Exception as e:
            current_app.logger.error(f'RESET_OTP:WHATSAPP_FAILED to={phone} error={e}')

    return results


@bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user_captcha = request.form.get('captcha', '').strip()

        # Validate CAPTCHA
        if user_captcha.upper() != session.get('captcha', '').upper():
            flash('Invalid CAPTCHA. Please try again.', 'danger')
            return redirect(url_for('auth.forgot_password'))

        if not email:
            flash('Please enter your email address.', 'danger')
            return redirect(url_for('auth.forgot_password'))

        # Check if user exists
        db = get_db()
        cursor = db.cursor()
        cursor.execute('SELECT customer_id, customer_name, customer_email, customer_phone, customer_password, google_id FROM customers WHERE customer_email = %s', (email,))
        user = cursor.fetchone()

        if user is None:
            flash('No account found with this email. Please register first.', 'danger')
            return redirect(url_for('auth.forgot_password'))

        # Only reject if account is actually linked to Google (has google_id)
        if user.get('google_id') and not user.get('customer_password'):
            flash('This account uses Google login. Please sign in with Google.', 'danger')
            return redirect(url_for('auth.login'))

        # Generate and send OTP
        otp = _generate_otp()
        session['_reset_otp'] = otp
        session['_reset_email'] = email
        session['_reset_otp_time'] = time.time()
        session['_reset_otp_attempts'] = 0

        phone = user.get('customer_phone', '')
        _send_reset_otp(email, otp, phone=phone)

        flash('OTP sent to your email address. Please check your inbox.', 'success')
        return redirect(url_for('auth.verify_reset_otp'))

    # GET request - generate captcha
    captcha_text = captcha_generator.generate_captcha()
    session['captcha'] = captcha_text
    captcha_image = captcha_generator.generate_captcha_image(captcha_text)
    return render_template('forgot_password.html', captcha_image=captcha_image, step='email')


@bp.route('/verify-reset-otp', methods=['GET', 'POST'])
def verify_reset_otp():
    # Must have a pending reset
    if '_reset_otp' not in session or '_reset_email' not in session:
        flash('Please request a password reset first.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    # Check OTP expiry (10 minutes)
    otp_time = session.get('_reset_otp_time', 0)
    if time.time() - otp_time > 600:
        session.pop('_reset_otp', None)
        session.pop('_reset_email', None)
        flash('OTP has expired. Please request a new one.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        user_otp = request.form.get('otp', '').strip()

        # Rate limit: max 5 attempts
        attempts = session.get('_reset_otp_attempts', 0)
        if attempts >= 5:
            session.pop('_reset_otp', None)
            session.pop('_reset_email', None)
            flash('Too many incorrect attempts. Please request a new OTP.', 'danger')
            return redirect(url_for('auth.forgot_password'))

        if user_otp != session.get('_reset_otp'):
            session['_reset_otp_attempts'] = attempts + 1
            flash(f'Incorrect OTP. {4 - attempts} attempts remaining.', 'danger')
            return redirect(url_for('auth.verify_reset_otp'))

        # OTP verified — allow password reset
        session['_reset_verified'] = True
        session.pop('_reset_otp', None)
        session.pop('_reset_otp_attempts', None)
        return redirect(url_for('auth.reset_password'))

    email = session.get('_reset_email', '')
    masked_email = email[:2] + '***' + email[email.index('@'):] if '@' in email else email
    return render_template('forgot_password.html', step='otp', masked_email=masked_email)


@bp.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    # Must have verified OTP
    if not session.get('_reset_verified') or not session.get('_reset_email'):
        flash('Please verify your OTP first.', 'danger')
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if len(password) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return redirect(url_for('auth.reset_password'))

        if password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('auth.reset_password'))

        # Update password in DB
        email = session.get('_reset_email')
        db = get_db()
        cursor = db.cursor()
        from . import ops_customers
        ops_customers.ensure_schema(db)
        cursor.execute('SELECT customer_id FROM customers WHERE customer_email = %s',
                       (email,))
        customer_ids = [row['customer_id'] for row in cursor.fetchall()]
        hashed_password = generate_password_hash(password)
        cursor.execute('UPDATE customers SET customer_password = %s WHERE customer_email = %s', (hashed_password, email))
        for customer_id in customer_ids:
            cursor.execute('DELETE FROM customer_temp_passwords WHERE customer_id=%s',
                           (customer_id,))
        db.commit()

        # Clear reset session data
        session.pop('_reset_verified', None)
        session.pop('_reset_email', None)
        session.pop('_reset_otp_time', None)

        current_app.logger.info(f'[{request.host}] ACTIVITY:PASSWORD_RESET email={email}')
        flash('Password reset successfully! Please log in with your new password.', 'success')
        return redirect(url_for('auth.login'))

    return render_template('forgot_password.html', step='reset')
