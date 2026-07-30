from flask import Flask, request, render_template, current_app, url_for, Blueprint, flash, redirect, session
import requests
import logging
from .mail import send_contact_email, create_ticket_in_db
from requests.auth import HTTPBasicAuth
from .captcha import CaptchaGenerator
from flask import current_app
from datetime import datetime
from flask_mail import Message
import os
import time
import json as json_mod
import smtplib
import socket
import hmac
import hashlib
import threading

bp = Blueprint('crm', __name__)
captcha_generator = CaptchaGenerator()

KET_API_URL = os.environ.get("KET_SUPPORT_URL", "https://support.ket.ltd/new/api/v1/external/messages")

# KET pushes ticket-lifecycle events (resolved) to this app, HMAC-SHA256 signed
# over "<timestamp>:<raw_body>" with the shared OPTIWAR_WEBHOOK_SECRET.
KET_SIGNATURE_MAX_SKEW = 300  # seconds; reject stale/replayed timestamps


def _verify_ket_signature(raw_body, timestamp, signature):
    """Verify a KET webhook HMAC-SHA256 signature. Fails closed on any problem."""
    secret = current_app.config.get('OPTIWAR_WEBHOOK_SECRET', '')
    if not secret or not timestamp or not signature:
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


def _notify_ticket_created(name, email, phone, ticket_id, subject):
    """Best-effort WhatsApp ack on ticket creation. Never breaks ticket creation.

    Optiwar owns WhatsApp directly (MSG91); KET only sends the customer email.
    Runs after KET forward + customer success so a notification hiccup cannot
    abort the ticket. Skips silently when no phone is available (anonymous).

    Gated OFF by default until resolved/reopened lifecycle acceptance passes
    (TICKET_CREATED_WHATSAPP_ENABLED).
    """
    if not current_app.config.get('TICKET_CREATED_WHATSAPP_ENABLED', False):
        return
    try:
        from .notifications import notify_support_ticket_created
        notify_support_ticket_created(
            customer_email=email,
            customer_phone=phone,
            customer_name=name,
            ticket_id=ticket_id,
            subject_text=subject or "Support request",
            site_host=request.host,
        )
    except Exception as e:  # noqa: BLE001 - notification is best-effort
        current_app.logger.error(f"[TICKET-WA] created notify failed ticket={ticket_id}: {e}")

# Customer-facing message shown when the AI provider is unavailable. The
# customer never sees a raw "AI unavailable" error; instead the same support
# interface transparently switches to direct ticket capture.
AI_FALLBACK_MESSAGE = (
    "We're temporarily experiencing high demand. I've already prepared your "
    "support request and our support team will continue assisting you. Please "
    "describe your issue below and we'll create a support ticket right away."
)

# Lightweight in-process health cache so the support entry can react to
# provider/backend outages without hammering dependencies on every request.
_HEALTH_CACHE = {"ts": 0.0, "data": None}
_HEALTH_TTL = 60  # seconds


def _check_ai_health():
    """Cheap AI-provider liveness probe. Returns (ok: bool, detail: str)."""
    key = os.environ.get('OPENAI_API_KEY', '')
    if not key:
        return False, "no_api_key"
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=4.0, max_retries=0)
        client.models.list()
        return True, "ok"
    except Exception as e:  # noqa: BLE001 - health probe must never raise
        return False, type(e).__name__


def _check_tcp_health(host, port, timeout=4.0):
    """Return (ok, detail) for a plain TCP reachability check."""
    if not host:
        return False, "not_configured"
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _check_smtp_health():
    """STARTTLS reachability check (no login) for the mail server."""
    host = current_app.config.get('MAIL_SERVER', '')
    port = current_app.config.get('MAIL_PORT', 587)
    if not host:
        return False, "not_configured"
    try:
        with smtplib.SMTP(host, int(port), timeout=5.0) as s:
            s.ehlo()
            if current_app.config.get('MAIL_USE_TLS'):
                s.starttls()
            return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _check_ket_health():
    """Reachability check for the KET support backend."""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(KET_API_URL)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        return _check_tcp_health(host, port)
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _support_health(force=False):
    """Return cached support-pipeline health. AI availability drives the UI."""
    now = time.time()
    if not force and _HEALTH_CACHE["data"] and (now - _HEALTH_CACHE["ts"]) < _HEALTH_TTL:
        return _HEALTH_CACHE["data"]

    ai_ok, ai_detail = _check_ai_health()
    ket_ok, ket_detail = _check_ket_health()
    smtp_ok, smtp_detail = _check_smtp_health()

    wa_configured = bool(os.environ.get('WHATSAPP_API_URL') or os.environ.get('WHATSAPP_TOKEN'))
    sms_configured = bool(os.environ.get('SMS_API_URL') or os.environ.get('SMS_API_KEY'))

    data = {
        "ai_available": ai_ok,
        "components": {
            "ai": {"ok": ai_ok, "detail": ai_detail},
            "ket": {"ok": ket_ok, "detail": ket_detail},
            "smtp": {"ok": smtp_ok, "detail": smtp_detail},
            "whatsapp": {"ok": wa_configured, "detail": "ok" if wa_configured else "not_configured"},
            "sms": {"ok": sms_configured, "detail": "ok" if sms_configured else "not_configured"},
        },
        "checked_at": int(now),
    }
    _HEALTH_CACHE["data"] = data
    _HEALTH_CACHE["ts"] = now
    return data


def _get_site_from():
    """Returns 'in.optiwar.com' or 'optiwar.com' based on request host."""
    try:
        host = request.host.lower()
        if 'in.optiwar.com' in host or 'optiwar.in' in host:
            return 'in.optiwar.com'
    except:
        pass
    return 'optiwar.com'


def _get_ket_site():
    """Returns 'site2' for in.optiwar.com, 'site1' for optiwar.com."""
    try:
        host = request.host.lower()
        if 'in.optiwar' in host or 'optiwar.in' in host:
            return 'site2'
    except:
        pass
    return 'site1'


def _ket_api_key():
    """Return the per-site KET API key based on the request host."""
    if _get_site_from() == 'in.optiwar.com':
        return os.environ.get('KET_SUPPORT_KEY_INOPTIWAR', '')
    return os.environ.get('KET_SUPPORT_KEY_OPTIWAR', '')


def _forward_to_ket(name, email, phone, subject, description, source="web_form", chat_transcript=None, session_id=None):
    """
    Push a contact-form or AI-chat event to KET Support via the new per-site
    push API (X-API-Key auth, POST /api/v1/external/messages).
    Fire-and-forget — never breaks existing flow.
    Returns KET ticket_id on success, None on failure.
    """
    try:
        # email is required by the KET API (must contain @); skip silently otherwise
        if not email or '@' not in email:
            logging.warning("KET push skipped: missing/invalid email")
            return None

        api_key = _ket_api_key()
        if not api_key:
            logging.warning("KET push skipped: no API key configured for this site")
            return None

        # Map legacy source values -> new schema (web_form | ai_chat) + triage category
        if source and source.startswith("ai_chat"):
            ket_source, category = "ai_chat", "service"
        else:
            ket_source, category = "web_form", "sales"

        payload = {
            "source": ket_source,
            "category": category,
            "email": email,
            "name": name or "",
            "phone": phone or "",
            "subject": subject or "Contact from Optiwar",
            "message": description or "",
        }
        if session_id:
            payload["session_id"] = session_id

        # transcript: accept a JSON string or a list; normalise to [{role,content}]
        if chat_transcript:
            try:
                tr = chat_transcript
                if isinstance(tr, str):
                    tr = json_mod.loads(tr)
                if isinstance(tr, list):
                    payload["transcript"] = [
                        {"role": m.get("role", ""), "content": m.get("content", "")}
                        for m in tr if isinstance(m, dict)
                    ]
            except Exception as e:
                logging.warning(f"KET transcript parse failed: {e}")

        # Bounded retry: KET rate-limits at 120 req/min/key and can return 5xx.
        # Retry only on 429/5xx with a short backoff (this runs in-request, so keep
        # the total added latency tight); never retry other 4xx (bad payload/auth).
        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            resp = requests.post(
                KET_API_URL,
                headers={"X-API-Key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=15,
            )
            if resp.status_code == 200:
                ket_ticket_id = resp.json().get('ticket_id')
                logging.info(f"KET ticket created: {ket_ticket_id}")
                return ket_ticket_id
            retryable = resp.status_code == 429 or 500 <= resp.status_code < 600
            logging.warning(f"KET push returned {resp.status_code}: {resp.text[:200]}")
            if not retryable or attempt == max_attempts:
                break
            backoff = 1.0
            if resp.status_code == 429:
                try:
                    backoff = min(float(resp.headers.get('Retry-After', 1)), 2.0)
                except (TypeError, ValueError):
                    backoff = 1.0
            time.sleep(backoff)
    except Exception as e:
        logging.error(f"KET push failed: {e}")
    return None



@bp.route('/contact_us', methods=['POST', 'GET'])
def create_ticket():

    if request.method == 'POST':
        if 'ticket_id' in session:
            ticket_id = session['ticket_id']
            message = f'Support ticket {ticket_id} is already submitted, please wait for our team to response.'
            logging.warning("Duplicate ticket generation prevention for ticket_id=%s", ticket_id)
            return render_template('contact_us.html', ticket_id=ticket_id, message=message)
        # Extract form data
        requester_name = request.form.get('name')
        subject = request.form.get('subject')
        description = request.form.get('description')
        requester_email = request.form.get('email')
        phone = request.form.get('phone')
        user_captcha = request.form.get('captcha')
        if user_captcha != session.get('captcha'):
           flash('Invalid Catpcha - Please carefully re-enter verification text ', 'danger')
           logging.info("Wrong captcha attempted ")
           return redirect(url_for('crm.create_ticket'))
        logging.info("Form submission received: name=%s, subject=%s, email=%s", requester_name, subject, requester_email)


        # Validate required fields
        if not subject or not description or not requester_email:
            logging.warning("Validation failed: missing required fields.")
            flash(
                'Please fill in complete details before submitting. '
                'We need full details for our team to quickly help you.',
                'warning'
            )
            return redirect(url_for('crm.create_ticket'))

        try:
            # Creating tickets into DB first
            ticket_id = create_ticket_in_db(
                name=requester_name,
                email=requester_email,
                subject=subject,
                message=description
            )


            # Send email to admin
            send_contact_email(
                name=requester_name,
                email=requester_email,
                subject=subject,
                phone=phone,
                message=description,
                ticket_id=ticket_id
            )

            # --- Forward to KET Support ---
            _forward_to_ket(
                name=requester_name,
                email=requester_email,
                phone=phone,
                subject=subject,
                description=description,
                source="web_form"
            )
            # --- End KET Support ---

            # WhatsApp ack (Optiwar-owned, best-effort, after KET forward)
            _notify_ticket_created(requester_name, requester_email, phone, ticket_id, subject)

            session['ticket_id'] = ticket_id
            logging.debug(f"Session contents: %s ", dict(session))
            return render_template('contact_us.html', ticket_id=ticket_id)

        except RuntimeError as e:
            logging.error("Error sending contact form email: %s", str(e), exc_info=True)
            flash(f"An error occurred while processing your request: {e}", 'danger')
            return redirect(url_for('crm.create_ticket'))

    if request.method == 'GET':
       captcha_text = captcha_generator.generate_captcha()
       session['captcha'] = captcha_text
       captcha_image = captcha_generator.generate_captcha_image(captcha_text)
       logging.info("Generated Captcha: %s", session['captcha'])




    if 'ticket_id' in session:
        session.pop('ticket_id', None)
        logging.info("Session reset for refeshed page ")

    # For GET requests, render the form
    logging.info("Rendering contact_us page ")
    return render_template('contact_us.html', ticket_id=None, message=None, captcha_image=captcha_image)




@bp.route('/contact_us/captcha', methods=['GET'])
def get_captcha():
    """AJAX endpoint to generate and return captcha image as JSON."""
    from flask import jsonify
    session.pop('ticket_id', None)
    captcha_text = captcha_generator.generate_captcha()
    session['captcha'] = captcha_text
    captcha_image = captcha_generator.generate_captcha_image(captcha_text)
    return jsonify({'captcha_image': captcha_image})


@bp.route('/contact_us/submit', methods=['POST'])
def submit_ticket_ajax():
    """AJAX endpoint to submit contact form and return JSON response."""
    from flask import jsonify
    try:
        if 'ticket_id' in session:
            ticket_id = session['ticket_id']
            return jsonify({'success': False, 'message': f'Support ticket {ticket_id} already submitted. Please wait for our team to respond.'}), 400

        requester_name = request.form.get('name')
        subject = request.form.get('subject')
        description = request.form.get('description')
        requester_email = request.form.get('email')
        phone = request.form.get('phone')
        user_captcha = request.form.get('captcha')

        if user_captcha != session.get('captcha'):
            return jsonify({'success': False, 'message': 'Invalid captcha. Please try again.', 'captcha_error': True}), 400

        if not subject or not description or not requester_email:
            return jsonify({'success': False, 'message': 'Please fill in all required fields.'}), 400

        ticket_id = create_ticket_in_db(
            name=requester_name,
            email=requester_email,
            subject=subject,
            message=description
        )

        send_contact_email(
            name=requester_name,
            email=requester_email,
            subject=subject,
            phone=phone,
            message=description,
            ticket_id=ticket_id
        )

        # --- Forward to KET Support ---
        _forward_to_ket(
            name=requester_name,
            email=requester_email,
            phone=phone,
            subject=subject,
            description=description,
            source="web_form"
        )
        # --- End KET Support ---

        # WhatsApp ack (Optiwar-owned, best-effort, after KET forward)
        _notify_ticket_created(requester_name, requester_email, phone, ticket_id, subject)

        session['ticket_id'] = ticket_id
        return jsonify({'success': True, 'ticket_id': ticket_id, 'message': f'Support ticket #{ticket_id} created successfully! Our team will respond shortly.'})

    except Exception as e:
        logging.error("Error in AJAX contact submission: %s", str(e), exc_info=True)
        return jsonify({'success': False, 'message': 'An error occurred. Please try again later.'}), 500


# ==================== AI CHAT SUPPORT ====================
@bp.route('/contact_us/ai_chat', methods=['POST'])
def ai_chat():
    """
    AI-assisted contact chat endpoint.
    Accepts JSON: {message: str, history: [{role, content}]}
    Returns JSON: {reply: str, done: bool, ticket_data: {...} | null}
    """
    from flask import jsonify, current_app
    from openai import OpenAI

    OPENAI_KEY = os.environ.get('OPENAI_API_KEY', '')

    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        user_msg = data.get("message", "").strip()
        history = data.get("history", [])

        if not user_msg:
            return jsonify({"error": "Empty message"}), 400

        # Layer 2 fallback: if the AI provider isn't even configured, don't
        # pretend — transparently switch the same interface to direct capture.
        if not OPENAI_KEY:
            return jsonify({
                "reply": AI_FALLBACK_MESSAGE,
                "done": False,
                "ticket_data": None,
                "ai_available": False,
            })

        # System prompt for the AI assistant
        system_prompt = """You are a quick, efficient customer support assistant for Optiwar (online eyeglasses store, India). Collect these details to create a support ticket:
- Name
- Email
- Phone (default +91)
- Subject (pick closest: "I want to order frames with lenses", "Requesting callback before ordering", or "Others")
- Description of their issue (1-2 sentences is enough)

CRITICAL RULES:
- The MOMENT you have name + email + phone + a clear issue/query, OUTPUT the ticket data immediately. Do NOT keep asking questions.
- If user gives most info in one message, just confirm and output the ticket.
- Maximum 3 exchanges before you MUST output the ticket with whatever info you have.
- Keep responses to 1-2 sentences max. Be fast and efficient.
- If only email is missing, ask ONLY for email. Never re-ask for info already given.

When you have enough info, end your message with EXACTLY this format (no extra text after it):
```TICKET_DATA
{"name": "...", "email": "...", "phone": "...", "subject": "...", "description": "..."}
```"""

        # Auto-identity: for logged-in customers the profile is already known,
        # so instruct the assistant never to re-ask for name/email/phone.
        # Trust the server session, not client-supplied identity.
        if session.get('user_id'):
            known_name = session.get('user_name', '')
            known_email = session.get('user_email', '')
            known_phone = session.get('user_phone', '')
            system_prompt += (
                "\n\nIMPORTANT — the customer is already logged in and their "
                "contact details are ALREADY KNOWN. Do NOT ask for name, email "
                "or phone; use these exactly as-is in the ticket data:\n"
                f"- Name: {known_name}\n"
                f"- Email: {known_email}\n"
                f"- Phone: {known_phone or '+91'}\n"
                "Only clarify the subject and the issue, then output the ticket."
            )

        # Build messages array
        messages = [{"role": "system", "content": system_prompt}]
        for h in history[-10:]:  # Keep last 10 messages for context
            messages.append({"role": h.get("role", "user"), "content": h.get("content", "")})
        messages.append({"role": "user", "content": user_msg})

        # Call GPT-3.5-turbo
        client = OpenAI(api_key=OPENAI_KEY)
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            max_tokens=300,
            temperature=0.7,
        )

        reply = response.choices[0].message.content.strip()

        # Check if ticket data is present in the reply
        ticket_data = None
        done = False
        if "TICKET_DATA" in reply:
            try:
                if "```TICKET_DATA" in reply:
                    json_start = reply.index("```TICKET_DATA") + len("```TICKET_DATA")
                    json_end = reply.index("```", json_start)
                    ticket_json = reply[json_start:json_end].strip()
                    reply = reply[:reply.index("```TICKET_DATA")].strip()
                else:
                    idx = reply.index("TICKET_DATA")
                    json_part = reply[idx + len("TICKET_DATA"):]
                    brace_start = json_part.index("{")
                    brace_end = json_part.rindex("}") + 1
                    ticket_json = json_part[brace_start:brace_end]
                    reply = reply[:idx].strip()
                ticket_data = json_mod.loads(ticket_json)
                done = True
                if not reply:
                    reply = "I have all the details. Let me create your support ticket now."
            except (ValueError, json_mod.JSONDecodeError):
                pass

        return jsonify({"reply": reply, "done": done, "ticket_data": ticket_data, "ai_available": True})

    except Exception as e:
        # Layer 2: any provider failure (outage, timeout, capacity, auth) must
        # never surface as "AI unavailable". Mark AI down so the same interface
        # switches to direct ticket capture and keeps helping the customer.
        current_app.logger.error(f"[AI-CHAT] Error: {e}", exc_info=True)
        _HEALTH_CACHE["data"] = None  # invalidate so the entry re-checks health
        return jsonify({
            "reply": AI_FALLBACK_MESSAGE,
            "done": False,
            "ticket_data": None,
            "ai_available": False,
        })


@bp.route('/contact_us/ai_submit', methods=['POST'])
def ai_submit_ticket():
    """
    Submit ticket from AI chat + log the conversation.
    Accepts JSON: {name, email, phone, subject, description, chat_history: [...]}
    """
    from flask import jsonify, current_app
    from .mail import create_ticket_in_db, send_contact_email
    from .db import get_db

    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No data provided"}), 400

        name = data.get("name", "").strip()
        email = data.get("email", "").strip()
        phone = data.get("phone", "").strip()
        subject = data.get("subject", "").strip()
        description = data.get("description", "").strip()
        chat_history = data.get("chat_history", [])

        # Auto-identity: for logged-in customers, always trust the session
        # profile over anything supplied by the client.
        if session.get('user_id'):
            name = session.get('user_name', '') or name
            email = session.get('user_email', '') or email
            phone = session.get('user_phone', '') or phone

        # Direct-capture (AI-down) tickets may not carry an AI-chosen subject.
        if not subject:
            subject = "Support request"

        if not email or not description:
            return jsonify({"error": "Please provide your email and a short description of the issue."}), 400

        # Create the ticket via existing route
        ticket_id = create_ticket_in_db(
            name=name,
            email=email,
            subject=subject,
            message=description
        )

        # Send notification email
        try:
            send_contact_email(
                name=name,
                email=email,
                subject=subject,
                phone=phone,
                message=f"[AI-Assisted] {description}",
                ticket_id=ticket_id
            )
        except Exception:
            pass  # Don't fail ticket creation if email fails

        # --- Forward to KET Support (with chat transcript) ---
        chat_transcript = json_mod.dumps(chat_history) if chat_history else None
        _forward_to_ket(
            name=name,
            email=email,
            phone=phone,
            subject=subject,
            description=f"[AI-Assisted] {description}",
            source="ai_chat",
            chat_transcript=chat_transcript
        )
        # --- End KET Support ---

        # WhatsApp ack (Optiwar-owned, best-effort, after KET forward)
        _notify_ticket_created(name, email, phone, ticket_id, subject)

        # Log AI chat to ai_chat_logs table
        try:
            db = get_db()
            cursor = db.cursor()
            ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
            cursor.execute("""
                INSERT INTO ai_chat_logs
                (ticket_id, customer_name, customer_email, customer_phone, subject, chat_json, status, ip_address, session_ended_at, site_from)
                VALUES (%s, %s, %s, %s, %s, %s, 'completed', %s, NOW(), %s)
            """, (ticket_id, name, email, phone, subject, json_mod.dumps(chat_history), ip_address, _get_site_from()))
            db.commit()
            cursor.close()
        except Exception as e:
            current_app.logger.error(f"[AI-CHAT] Failed to log chat: {e}")

        current_app.logger.info(f"[AI-CHAT] Ticket #{ticket_id} created for {email} via AI chat")

        return jsonify({
            "success": True,
            "ticket_id": ticket_id,
            "message": f"Support ticket #{ticket_id} created successfully!"
        })

    except Exception as e:
        current_app.logger.error(f"[AI-CHAT] Submit error: {e}", exc_info=True)
        return jsonify({"error": "We couldn't submit your request just now. Please try again in a moment."}), 500


@bp.route('/support/status', methods=['GET'])
def support_status():
    """
    Lightweight support-pipeline health for the single support entry point.
    The customer-facing widget calls this to decide whether to greet with the
    AI assistant or transparently switch to direct ticket capture.
    """
    from flask import jsonify
    try:
        return jsonify(_support_health())
    except Exception as e:  # noqa: BLE001 - status must never raise
        current_app.logger.error(f"[SUPPORT-STATUS] {e}", exc_info=True)
        # Fail safe: assume AI is down so support degrades to direct capture.
        return jsonify({"ai_available": False, "components": {}, "checked_at": int(time.time())})


# ---------------------------------------------------------------------------
# KET lifecycle webhook v3 (schema version 1)
#
# Contract: persist-first-then-ack, dedupe strictly on event_id, MSG91 sent
# asynchronously so a slow provider never consumes KET's webhook timeout, and a
# separate per-channel key (ket:<event_id>:whatsapp) guarantees the customer is
# never messaged twice across KET retries / worker restarts.
# ---------------------------------------------------------------------------
KET_SCHEMA_VERSION = 1

# Actioned events -> the MSG91 template Optiwar sends from its own number.
# Unknown/future events (waiting_customer, closed, ...) are logged + 200-ignored
# so KET can expand the lifecycle without breaking anything.
_KET_EVENT_TEMPLATES = {
    'resolved': 'support_ticket_resolved',
    'reopened': 'support_ticket_reopened',
}

_KET_SCHEMA_READY = False


def _ensure_ket_schema(cur):
    """Create the lifecycle + WhatsApp-delivery tables once per process."""
    global _KET_SCHEMA_READY
    if _KET_SCHEMA_READY:
        return
    cur.execute(
        """CREATE TABLE IF NOT EXISTS ket_lifecycle_events (
               event_id           VARCHAR(191) NOT NULL,
               version            INT DEFAULT 1,
               event              VARCHAR(64),
               ticket_id          VARCHAR(191),
               ticket_ref         VARCHAR(191),
               request_id         VARCHAR(191),
               name               VARCHAR(191),
               phone              VARCHAR(64),
               email              VARCHAR(191),
               signature_verified TINYINT(1) DEFAULT 0,
               processing_status  VARCHAR(32) DEFAULT 'stored',
               received_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (event_id)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS whatsapp_delivery_log (
               dedupe_key       VARCHAR(255) NOT NULL,
               event_id         VARCHAR(191),
               ticket_ref       VARCHAR(191),
               template_name    VARCHAR(64),
               recipient        VARCHAR(64),
               msg91_request_id VARCHAR(191),
               status           VARCHAR(32) DEFAULT 'pending',
               attempt_count    INT DEFAULT 0,
               last_error       TEXT,
               sent_at          DATETIME NULL,
               delivered_at     DATETIME NULL,
               created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (dedupe_key)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    _KET_SCHEMA_READY = True


def _store_lifecycle_event(event_id, version, event, ticket_id, ticket_ref,
                           request_id, name, phone, email, signature_verified):
    """Atomically claim + durably store a lifecycle event, keyed on event_id.

    Returns 'claimed' (first time), 'duplicate' (event_id already stored), or
    'error' (could not persist -> caller must NOT ack; KET should retry).
    INSERT IGNORE makes the claim race-safe: rowcount 1 == we own this event.
    """
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        _ensure_ket_schema(cur)
        cur.execute(
            """INSERT IGNORE INTO ket_lifecycle_events
                 (event_id, version, event, ticket_id, ticket_ref, request_id,
                  name, phone, email, signature_verified)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (event_id, version, event, ticket_id, ticket_ref, request_id,
             name, phone, email, 1 if signature_verified else 0),
        )
        claimed = cur.rowcount == 1
        db.commit()
        cur.close()
        return 'claimed' if claimed else 'duplicate'
    except Exception as e:  # noqa: BLE001 - durable store failed
        current_app.logger.error(f"[KET-EVENT] store failed event_id={event_id}: {e}")
        return 'error'


def _set_lifecycle_status(event_id, status):
    """Best-effort update of a lifecycle event's processing_status."""
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "UPDATE ket_lifecycle_events SET processing_status=%s WHERE event_id=%s",
            (status, event_id),
        )
        db.commit()
        cur.close()
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-EVENT] status update failed event_id={event_id}: {e}")


def _claim_whatsapp_send(dedupe_key, event_id, ticket_ref, template_name, recipient):
    """Per-channel idempotency claim. True only the first time for this key."""
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """INSERT IGNORE INTO whatsapp_delivery_log
                 (dedupe_key, event_id, ticket_ref, template_name, recipient,
                  status, attempt_count)
               VALUES (%s,%s,%s,%s,%s,'pending',1)""",
            (dedupe_key, event_id, ticket_ref, template_name, recipient),
        )
        claimed = cur.rowcount == 1
        db.commit()
        cur.close()
        return claimed
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-EVENT] whatsapp claim failed key={dedupe_key}: {e}")
        return False


def _record_whatsapp_result(dedupe_key, result):
    """Persist the MSG91 outcome (request id / status / error) for the audit log."""
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """UPDATE whatsapp_delivery_log
                 SET msg91_request_id=%s, status=%s, last_error=%s,
                     sent_at=CASE WHEN %s='sent' THEN NOW() ELSE sent_at END
               WHERE dedupe_key=%s""",
            (result.get('request_id', ''), result.get('status', ''),
             result.get('error', ''), result.get('status', ''), dedupe_key),
        )
        db.commit()
        cur.close()
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-EVENT] whatsapp result record failed key={dedupe_key}: {e}")


def _process_lifecycle_whatsapp(app, event, event_id, ticket_ref, name, phone, email, site_host):
    """Async worker: send the lifecycle WhatsApp exactly once via MSG91.

    Runs after the webhook has already acked KET, so MSG91 latency/failure never
    affects the webhook response. Idempotent on ket:<event_id>:whatsapp.
    """
    with app.app_context():
        try:
            if not phone:
                _set_lifecycle_status(event_id, 'skipped_no_phone')
                app.logger.info(f"[KET-EVENT] {event} no phone; skipped event_id={event_id} ticket={ticket_ref}")
                return
            template_name = _KET_EVENT_TEMPLATES[event]
            recipient = phone.replace('+', '').replace(' ', '').replace('-', '')
            dedupe_key = f"ket:{event_id}:whatsapp"
            if not _claim_whatsapp_send(dedupe_key, event_id, ticket_ref, template_name, recipient):
                app.logger.info(f"[KET-EVENT] whatsapp already claimed key={dedupe_key}; not resending")
                return
            from .notifications import send_whatsapp_tracked
            components = {
                "body_1": {"type": "text", "value": name or "Customer"},
                "body_2": {"type": "text", "value": str(ticket_ref)},
                "body_3": {"type": "text", "value": site_host},
            }
            result = send_whatsapp_tracked(recipient, template_name, components)
            _record_whatsapp_result(dedupe_key, result)
            _set_lifecycle_status(event_id, 'notified' if result.get('ok') else 'notify_failed')
            app.logger.info(
                f"[KET-EVENT] {event} whatsapp event_id={event_id} ticket={ticket_ref} "
                f"ok={result.get('ok')} rid={result.get('request_id') or '-'}"
            )
        except Exception as e:  # noqa: BLE001 - worker must never crash the process
            app.logger.error(f"[KET-EVENT] worker failed event_id={event_id}: {e}")


@bp.route('/support/ticket_event', methods=['POST'])
def ket_ticket_event():
    """Inbound KET ticket-lifecycle webhook (schema v1: resolved / reopened).

    KET is the system of record for the lifecycle; when an agent resolves/reopens
    a ticket KET calls this endpoint so Optiwar sends the WhatsApp update from its
    own MSG91 number (Optiwar owns WhatsApp; KET owns only the support email).

    Auth: HMAC-SHA256 over "<X-KET-Timestamp>:<raw_body>" with OPTIWAR_WEBHOOK_SECRET.
    JSON: {version?, event, event_id, ticket_ref, ticket_id?, request_id?, name, phone, email}

    Flow: verify signature -> validate schema -> durably store (atomic claim on
    event_id) -> ack -> process MSG91 asynchronously.
    Codes: 401 bad signature; 400 invalid schema; 200 accepted/duplicate/ignored;
    503 when the event could not be durably stored (KET should retry).
    """
    from flask import jsonify, g
    raw_body = request.get_data(as_text=True)
    if not _verify_ket_signature(
        raw_body,
        request.headers.get('X-KET-Timestamp'),
        request.headers.get('X-KET-Signature'),
    ):
        current_app.logger.warning("[KET-EVENT] rejected: invalid signature")
        return jsonify({"error": "invalid signature"}), 401

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid json body"}), 400

    try:
        version = int(data.get('version', KET_SCHEMA_VERSION))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid version"}), 400

    event = (data.get('event') or '').strip().lower()
    event_id = str(data.get('event_id') or '').strip()
    ticket_ref = str(data.get('ticket_ref') or data.get('ticket_id') or '').strip()
    ticket_id = str(data.get('ticket_id') or '').strip()
    request_id = str(data.get('request_id') or '').strip()
    name = (data.get('name') or '').strip()
    phone = (data.get('phone') or '').strip()
    email = (data.get('email') or '').strip()
    rid = getattr(g, 'request_id', '-')

    current_app.logger.info(
        f"[KET-EVENT] recv v={version} event={event or '-'} ticket_ref={ticket_ref or '-'} "
        f"ticket_id={ticket_id or '-'} event_id={event_id or '-'} req_id={request_id or '-'} "
        f"has_phone={bool(phone)} rid={rid}"
    )

    if version > KET_SCHEMA_VERSION:
        current_app.logger.warning(f"[KET-EVENT] newer schema v={version} (max {KET_SCHEMA_VERSION}); processing leniently")

    if not event:
        return jsonify({"error": "event required"}), 400

    if event not in _KET_EVENT_TEMPLATES:
        # Future lifecycle stages: ack without action so KET can expand freely.
        current_app.logger.info(f"[KET-EVENT] ignored (unhandled) event={event} ticket_ref={ticket_ref} rid={rid}")
        return jsonify({"status": "ignored", "event": event})

    # Schema validation for actioned events.
    if not event_id:
        return jsonify({"error": "event_id required"}), 400
    if not ticket_ref:
        return jsonify({"error": "ticket_ref required"}), 400

    # Persist first (atomic claim on event_id), THEN ack.
    outcome = _store_lifecycle_event(
        event_id, version, event, ticket_id, ticket_ref, request_id,
        name, phone, email, signature_verified=True,
    )
    if outcome == 'error':
        # Could not durably store -> do not ack; KET should retry.
        return jsonify({"error": "temporary storage failure"}), 503
    if outcome == 'duplicate':
        current_app.logger.info(f"[KET-EVENT] duplicate event_id={event_id} event={event} ticket_ref={ticket_ref}")
        return jsonify({"status": "duplicate", "event": event, "ticket_ref": ticket_ref})

    # Stored & claimed -> ack immediately, process MSG91 out of band.
    app_obj = current_app._get_current_object()
    threading.Thread(
        target=_process_lifecycle_whatsapp,
        args=(app_obj, event, event_id, ticket_ref, name, phone, email, request.host),
        daemon=True,
    ).start()

    return jsonify({"status": "accepted", "event": event, "ticket_ref": ticket_ref, "event_id": event_id}), 200
