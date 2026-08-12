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

# KET pushes ticket-lifecycle events (resolved/reopened) to this app, HMAC-SHA256
# signed over "<timestamp>:<raw_body>" with the shared OPTIWAR_WEBHOOK_SECRET.
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
    Runs after KET forward so a notification hiccup cannot abort the ticket.
    Skips silently when no phone is available (anonymous).

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
    Returns a dict {ticket_id, ticket_ref, ticket_uid} on success, None on failure.
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
                j = resp.json()
                # KET is standardising on an immutable UUID as the join key. The
                # create response returns the human ref today (ticket_id) and will
                # expose the UUID; read it defensively so mapping is uid-ready.
                result = {
                    "ticket_id": j.get('ticket_id'),
                    "ticket_ref": j.get('ticket_ref') or j.get('ticket_id'),
                    "ticket_uid": j.get('ticket_uid') or j.get('uid') or j.get('uuid'),
                }
                logging.info(
                    f"KET ticket created: id={result['ticket_id']} "
                    f"uid={result['ticket_uid'] or '-'}"
                )
                return result
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


            # --- Forward to KET Support (system of record) ---
            ket = _forward_to_ket(
                name=requester_name,
                email=requester_email,
                phone=phone,
                subject=subject,
                description=description,
                source="web_form"
            )
            if ket:
                persist_ticket_mapping(ticket_id, ket["ticket_id"], source_system="web_form",
                                       ket_uid=ket["ticket_uid"], ket_ref=ket["ticket_ref"])
            # --- End KET Support ---

            # WhatsApp ack (Optiwar-owned, best-effort, after KET forward)
            _notify_ticket_created(requester_name, requester_email, phone, ticket_id, subject)

            session['ticket_id'] = ticket_id
            logging.debug(f"Session contents: %s ", dict(session))

            # Internal admin notification: best-effort, out-of-transaction.
            # Never allowed to block ticket creation / customer confirmation.
            send_contact_email(
                name=requester_name,
                email=requester_email,
                subject=subject,
                phone=phone,
                message=description,
                ticket_id=ticket_id
            )

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

        # --- Forward to KET Support (system of record) ---
        ket = _forward_to_ket(
            name=requester_name,
            email=requester_email,
            phone=phone,
            subject=subject,
            description=description,
            source="web_form"
        )
        if ket:
            persist_ticket_mapping(ticket_id, ket["ticket_id"], source_system="web_form",
                                   ket_uid=ket["ticket_uid"], ket_ref=ket["ticket_ref"])
        # --- End KET Support ---

        # WhatsApp ack (Optiwar-owned, best-effort, after KET forward)
        _notify_ticket_created(requester_name, requester_email, phone, ticket_id, subject)

        session['ticket_id'] = ticket_id

        # Internal admin notification: best-effort, out-of-transaction.
        send_contact_email(
            name=requester_name,
            email=requester_email,
            subject=subject,
            phone=phone,
            message=description,
            ticket_id=ticket_id
        )

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

        # --- Forward to KET Support (with chat transcript) ---
        chat_transcript = json_mod.dumps(chat_history) if chat_history else None
        ket = _forward_to_ket(
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

        # Internal admin notification: best-effort, out-of-transaction.
        send_contact_email(
            name=name,
            email=email,
            subject=subject,
            phone=phone,
            message=f"[AI-Assisted] {description}",
            ticket_id=ticket_id
        )

        # Log AI chat to ai_chat_logs table
        ai_chat_log_id = None
        try:
            db = get_db()
            cursor = db.cursor()
            ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
            cursor.execute("""
                INSERT INTO ai_chat_logs
                (ticket_id, customer_name, customer_email, customer_phone, subject, chat_json, status, ip_address, session_ended_at, site_from)
                VALUES (%s, %s, %s, %s, %s, %s, 'completed', %s, NOW(), %s)
            """, (ticket_id, name, email, phone, subject, json_mod.dumps(chat_history), ip_address, _get_site_from()))
            ai_chat_log_id = cursor.lastrowid
            db.commit()
            cursor.close()
        except Exception as e:
            current_app.logger.error(f"[AI-CHAT] Failed to log chat: {e}")

        # Persist the authoritative KET<->Optiwar bridge (Option A) linking the
        # chat session so the lifecycle receiver can close the correct session.
        if ket:
            persist_ticket_mapping(ticket_id, ket["ticket_id"],
                                   ai_chat_log_id=ai_chat_log_id, source_system="ai_chat",
                                   ket_uid=ket["ticket_uid"], ket_ref=ket["ticket_ref"])

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

# Durable WhatsApp delivery policy (restart-safe outbox worker).
KET_MAX_BODY_BYTES = 64 * 1024      # reject oversized payloads with 413
WA_MAX_ATTEMPTS = 5                 # after this a job is marked 'dead'
WA_LOCK_TIMEOUT = 120               # seconds; reclaim a stuck 'sending' row
WA_BACKOFF_BASE = 60                # seconds; exponential retry backoff base
WA_BACKOFF_CAP = 3600               # seconds; backoff ceiling
WA_SCAN_INTERVAL = 30              # seconds between outbox scans

_KET_SCHEMA_READY = False


def _ensure_ket_schema(cur):
    """Create the lifecycle + WhatsApp-outbox + delivery-event tables once."""
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
               body_name        VARCHAR(191),
               site_host        VARCHAR(191),
               msg91_request_id VARCHAR(191),
               status           VARCHAR(32) DEFAULT 'pending',
               attempt_count    INT DEFAULT 0,
               last_error       TEXT,
               next_attempt_at  DATETIME NULL,
               locked_at        DATETIME NULL,
               sent_at          DATETIME NULL,
               delivered_at     DATETIME NULL,
               created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (dedupe_key)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS msg91_delivery_events (
               id               BIGINT AUTO_INCREMENT PRIMARY KEY,
               msg91_request_id VARCHAR(191),
               event_id         VARCHAR(191),
               ticket_ref       VARCHAR(191),
               recipient        VARCHAR(64),
               template_name    VARCHAR(64),
               status           VARCHAR(32),
               failure_reason   VARCHAR(255),
               provider_ts      VARCHAR(64),
               received_at      DATETIME DEFAULT CURRENT_TIMESTAMP
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    # Append-only operational audit trail (Phase C): one row per notable action,
    # never updated or deleted, so the full lifecycle is reconstructable.
    cur.execute(
        """CREATE TABLE IF NOT EXISTS support_event_audit (
               id            BIGINT AUTO_INCREMENT PRIMARY KEY,
               occurred_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
               kind          VARCHAR(48),
               event         VARCHAR(64),
               ticket_ref    VARCHAR(191),
               ticket_id     VARCHAR(191),
               event_id      VARCHAR(191),
               request_id    VARCHAR(191),
               sig_verified  TINYINT(1) DEFAULT 0,
               whatsapp_status VARCHAR(32),
               sms_status    VARCHAR(32),
               detail        VARCHAR(255),
               KEY idx_ticket (ticket_ref),
               KEY idx_kind (kind),
               KEY idx_time (occurred_at)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    # Per-customer notification channel preferences (Phase C). Defaults are ON;
    # a row only exists once a customer opts out of a channel.
    cur.execute(
        """CREATE TABLE IF NOT EXISTS notification_preferences (
               customer_key  VARCHAR(191) NOT NULL,
               email_enabled    TINYINT(1) DEFAULT 1,
               whatsapp_enabled TINYINT(1) DEFAULT 1,
               sms_enabled      TINYINT(1) DEFAULT 0,
               push_enabled     TINYINT(1) DEFAULT 0,
               updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
               PRIMARY KEY (customer_key)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    # Authoritative bridge between a KET ticket and the Optiwar ticket / chat
    # session (Option A). Populated at ticket creation from the id KET returns;
    # the lifecycle receiver joins on the immutable ket_ticket_id to close the
    # correct chat session. Never changes the frozen v1 inbound webhook contract.
    cur.execute(
        """CREATE TABLE IF NOT EXISTS optiwar_ticket_mapping (
               id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
               optiwar_ticket_id  VARCHAR(191) NOT NULL,
               ket_ticket_id      VARCHAR(191) NOT NULL,
               ket_ticket_uid     VARCHAR(191) NULL,
               ket_ticket_ref     VARCHAR(191) NULL,
               request_id         VARCHAR(191),
               ai_chat_log_id     BIGINT NULL,
               mapping_version    INT DEFAULT 1,
               created_by         VARCHAR(64) DEFAULT 'optiwar',
               source_system      VARCHAR(64) DEFAULT '',
               last_lifecycle_event VARCHAR(64),
               last_event_id      VARCHAR(191),
               last_event_at      DATETIME NULL,
               created_at         DATETIME DEFAULT CURRENT_TIMESTAMP,
               updated_at         DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
               UNIQUE KEY uq_ket (ket_ticket_id),
               UNIQUE KEY uq_optiwar (optiwar_ticket_id),
               UNIQUE KEY uq_uid (ket_ticket_uid),
               KEY idx_request (request_id),
               KEY idx_ref (ket_ticket_ref)
           ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    )
    # Additive migration for tables created before the UUID join key existed.
    for ddl in (
        "ALTER TABLE optiwar_ticket_mapping ADD COLUMN ket_ticket_uid VARCHAR(191) NULL",
        "ALTER TABLE optiwar_ticket_mapping ADD COLUMN ket_ticket_ref VARCHAR(191) NULL",
        "ALTER TABLE optiwar_ticket_mapping ADD UNIQUE KEY uq_uid (ket_ticket_uid)",
        "ALTER TABLE optiwar_ticket_mapping ADD KEY idx_ref (ket_ticket_ref)",
    ):
        try:
            cur.execute(ddl)
        except Exception:  # noqa: BLE001 - column/key already exists
            pass
    _KET_SCHEMA_READY = True


def _audit(kind, event='', ticket_ref='', ticket_id='', event_id='', request_id='',
           sig_verified=False, whatsapp_status='', sms_status='', detail=''):
    """Append-only audit write. Best-effort: never affects the caller's response."""
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        _ensure_ket_schema(cur)
        cur.execute(
            """INSERT INTO support_event_audit
                 (kind, event, ticket_ref, ticket_id, event_id, request_id,
                  sig_verified, whatsapp_status, sms_status, detail)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (kind[:48], event[:64], ticket_ref[:191], ticket_id[:191], event_id[:191],
             request_id[:191], 1 if sig_verified else 0, whatsapp_status[:32],
             sms_status[:32], detail[:255]),
        )
        db.commit()
        cur.close()
    except Exception as e:  # noqa: BLE001 - audit must never break the request
        current_app.logger.error(f"[KET-AUDIT] write failed kind={kind}: {e}")


def _whatsapp_pref_allowed(customer_key):
    """True unless the customer has explicitly disabled WhatsApp. Fails open."""
    if not customer_key:
        return True
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "SELECT whatsapp_enabled FROM notification_preferences WHERE customer_key=%s",
            (customer_key,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return True
        return bool(row["whatsapp_enabled"])
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-PREF] read failed key={customer_key}: {e}")
        return True


def persist_ticket_mapping(optiwar_ticket_id, ket_ticket_id, request_id='',
                           ai_chat_log_id=None, source_system='',
                           ket_uid='', ket_ref=''):
    """Persist the KET<->Optiwar bridge at ticket creation (Option A).

    The immutable KET UUID (ket_ticket_uid) is the authoritative join key once
    KET exposes it; ket_ticket_ref is diagnostic-only. Idempotent upsert keyed on
    a unique id. Best-effort: a mapping failure must never break ticket creation.
    """
    if not optiwar_ticket_id or not ket_ticket_id:
        return
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        _ensure_ket_schema(cur)
        cur.execute(
            """INSERT INTO optiwar_ticket_mapping
                 (optiwar_ticket_id, ket_ticket_id, ket_ticket_uid, ket_ticket_ref,
                  request_id, ai_chat_log_id, mapping_version, created_by, source_system)
               VALUES (%s,%s,%s,%s,%s,%s,1,'optiwar',%s)
               ON DUPLICATE KEY UPDATE
                 ket_ticket_id=VALUES(ket_ticket_id),
                 ket_ticket_uid=COALESCE(VALUES(ket_ticket_uid), ket_ticket_uid),
                 ket_ticket_ref=COALESCE(VALUES(ket_ticket_ref), ket_ticket_ref),
                 request_id=VALUES(request_id),
                 ai_chat_log_id=COALESCE(VALUES(ai_chat_log_id), ai_chat_log_id),
                 source_system=VALUES(source_system)""",
            (str(optiwar_ticket_id)[:191], str(ket_ticket_id)[:191],
             (str(ket_uid)[:191] or None), (str(ket_ref)[:191] or None),
             str(request_id or '')[:191], ai_chat_log_id, str(source_system or '')[:64]),
        )
        db.commit()
        cur.close()
        current_app.logger.info(
            f"[KET-MAP] mapped optiwar_ticket_id={optiwar_ticket_id} "
            f"ket_ticket_id={ket_ticket_id} ket_uid={ket_uid or '-'} "
            f"ai_chat_log_id={ai_chat_log_id}"
        )
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-MAP] persist failed optiwar={optiwar_ticket_id}: {e}")


def _lookup_ticket_mapping(ticket_id, ticket_ref):
    """Find the bridge row for a lifecycle event.

    Authoritative join is the immutable UUID: the lifecycle webhook's ticket_id
    matched against ket_ticket_uid. Falls back to ket_ticket_id / ket_ticket_ref
    (diagnostic) so the ref-based join keeps working until KET exposes the UUID.
    Returns (row, matched_by) where matched_by is 'uid' or 'ref'."""
    from .db import get_db
    cols = "id, optiwar_ticket_id, ket_ticket_id, ket_ticket_uid, ai_chat_log_id"
    try:
        db = get_db()
        cur = db.cursor()
        _ensure_ket_schema(cur)
        # 1) Authoritative: UUID join on ket_ticket_uid.
        if ticket_id:
            cur.execute(
                f"SELECT {cols} FROM optiwar_ticket_mapping WHERE ket_ticket_uid=%s LIMIT 1",
                (ticket_id,),
            )
            row = cur.fetchone()
            if row:
                cur.close()
                return row, 'uid'
        # 2) Diagnostic fallback: ref / legacy id match.
        candidates = [c for c in {ticket_id, ticket_ref} if c]
        if candidates:
            ph = ','.join(['%s'] * len(candidates))
            cur.execute(
                f"""SELECT {cols} FROM optiwar_ticket_mapping
                     WHERE ket_ticket_ref IN ({ph})
                        OR ket_ticket_id IN ({ph})
                        OR optiwar_ticket_id IN ({ph})
                     LIMIT 1""",
                (*candidates, *candidates, *candidates),
            )
            row = cur.fetchone()
            cur.close()
            if row:
                return row, 'ref'
            return None, None
        cur.close()
        return None, None
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-MAP] lookup failed ticket_id={ticket_id}: {e}")
        return None, None


def _touch_mapping_lifecycle(mapping_id, event, event_id):
    """Record the most recent lifecycle event on the bridge row (auditability)."""
    if not mapping_id:
        return
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            """UPDATE optiwar_ticket_mapping
                  SET last_lifecycle_event=%s, last_event_id=%s, last_event_at=NOW()
                WHERE id=%s""",
            (str(event)[:64], str(event_id)[:191], mapping_id),
        )
        db.commit()
        cur.close()
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-MAP] touch failed id={mapping_id}: {e}")


def _close_chat_session(ai_chat_log_id):
    """Idempotently close the mapped chat session. Returns 'closed', 'noop'
    (already closed) or 'no_session' (nothing to close)."""
    if not ai_chat_log_id:
        return 'no_session'
    from .db import get_db
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT status FROM ai_chat_logs WHERE id=%s", (ai_chat_log_id,))
        row = cur.fetchone()
        if not row:
            cur.close()
            return 'no_session'
        if (row["status"] or '').lower() == 'closed':
            cur.close()
            return 'noop'
        cur.execute("UPDATE ai_chat_logs SET status='closed' WHERE id=%s", (ai_chat_log_id,))
        db.commit()
        cur.close()
        return 'closed'
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-MAP] close session failed id={ai_chat_log_id}: {e}")
        return 'noop'


def _process_session_lifecycle(event, ticket_id, ticket_ref, request_id, event_id):
    """Optiwar-owned session side-effects for a lifecycle event (Option A / B).

    resolved -> idempotently close the mapped chat session.
    reopened -> record lifecycle state only; transcript is preserved and NO new
                session is created / auto-reopened (Option B).
    A missing mapping is an operational alert (session_not_found), never a
    transport failure -- the lifecycle row is already durably stored.
    """
    mapping, matched_by = _lookup_ticket_mapping(ticket_id, ticket_ref)
    if not mapping:
        current_app.logger.warning(
            f"[KET-MAP] session_not_found ticket_id={ticket_id or '-'} "
            f"ticket_ref={ticket_ref or '-'} request_id={request_id or '-'} "
            f"event_id={event_id or '-'} reason=session_not_found"
        )
        _audit('session_not_found', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True,
               detail='no mapping row')
        return 'session_not_found'

    if matched_by == 'ref':
        # All tickets created after the UUID rollout must join by uid; a ref
        # match means a pre-UUID/legacy row -> operational alert during observation.
        current_app.logger.warning(
            f"[KET-MAP] ALERT ref_fallback_join used ticket_id={ticket_id or '-'} "
            f"ticket_ref={ticket_ref or '-'} event_id={event_id or '-'} "
            f"(expected join=uid)"
        )
        _audit('ref_fallback_join', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True,
               detail='diagnostic ref fallback used; expected uid join')

    _touch_mapping_lifecycle(mapping["id"], event, event_id)

    if event == 'resolved':
        result = _close_chat_session(mapping["ai_chat_log_id"])
        _audit('session_close', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True,
               detail=f"{result} (join={matched_by})")
        current_app.logger.info(
            f"[KET-MAP] resolved ket_ticket_id={ticket_id} ai_chat_log_id="
            f"{mapping['ai_chat_log_id']} session={result} join={matched_by}"
        )
        return result

    # reopened: lifecycle state only, transcript preserved, no session mutation.
    _audit('session_reopen', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
           event_id=event_id, request_id=request_id, sig_verified=True,
           detail='lifecycle only; transcript preserved; no new session')
    current_app.logger.info(
        f"[KET-MAP] reopened ket_ticket_id={ticket_id} lifecycle-only (no session reopen)"
    )
    return 'reopened_lifecycle_only'


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


def _enqueue_whatsapp_job(dedupe_key, event_id, ticket_ref, template_name,
                          recipient, body_name, site_host):
    """Durably enqueue a WhatsApp send (per-channel idempotent on dedupe_key).

    Returns True if a new job row was created. INSERT IGNORE means a KET retry
    with the same event_id never creates a second job -> never a second message.
    Raises on real DB failure so the caller can refuse to ack.
    """
    from .db import get_db
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """INSERT IGNORE INTO whatsapp_delivery_log
             (dedupe_key, event_id, ticket_ref, template_name, recipient,
              body_name, site_host, status, attempt_count, next_attempt_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',0,NOW())""",
        (dedupe_key, event_id, ticket_ref, template_name, recipient,
         body_name, site_host),
    )
    created = cur.rowcount == 1
    db.commit()
    cur.close()
    return created


def _claim_due_whatsapp_job(dedupe_key):
    """Atomically claim one due job for sending. Returns row dict or None.

    A conditional UPDATE (status pending/failed and due, or a 'sending' row whose
    lock has expired) with rowcount==1 guarantees only one worker owns the job,
    so concurrent workers / multiple gunicorn processes cannot double-send.
    """
    from .db import get_db
    db = get_db()
    cur = db.cursor()
    cur.execute(
        f"""UPDATE whatsapp_delivery_log
              SET status='sending', locked_at=NOW(), attempt_count=attempt_count+1
            WHERE dedupe_key=%s
              AND attempt_count < {WA_MAX_ATTEMPTS}
              AND (
                    ((status='pending' OR status='failed')
                       AND (next_attempt_at IS NULL OR next_attempt_at<=NOW()))
                    OR (status='sending'
                       AND locked_at < (NOW() - INTERVAL {WA_LOCK_TIMEOUT} SECOND))
                  )""",
        (dedupe_key,),
    )
    claimed = cur.rowcount == 1
    db.commit()
    if not claimed:
        cur.close()
        return None
    cur.execute(
        """SELECT event_id, ticket_ref, template_name, recipient, body_name,
                  site_host, attempt_count
             FROM whatsapp_delivery_log WHERE dedupe_key=%s""",
        (dedupe_key,),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {
        "event_id": row["event_id"], "ticket_ref": row["ticket_ref"],
        "template_name": row["template_name"], "recipient": row["recipient"],
        "body_name": row["body_name"], "site_host": row["site_host"],
        "attempt_count": row["attempt_count"],
    }


def _finalize_whatsapp_job(dedupe_key, event_id, result, attempt_count):
    """Record the send outcome + set the next state (sent / failed+retry / dead)."""
    from .db import get_db
    db = get_db()
    cur = db.cursor()
    if result.get("ok"):
        cur.execute(
            """UPDATE whatsapp_delivery_log
                 SET status='sent', msg91_request_id=%s, last_error='',
                     locked_at=NULL, sent_at=NOW()
               WHERE dedupe_key=%s""",
            (result.get("request_id", ""), dedupe_key),
        )
        _set_lifecycle_status(event_id, 'notified')
    elif attempt_count >= WA_MAX_ATTEMPTS:
        cur.execute(
            """UPDATE whatsapp_delivery_log
                 SET status='dead', msg91_request_id=%s, last_error=%s, locked_at=NULL
               WHERE dedupe_key=%s""",
            (result.get("request_id", ""), result.get("error", "")[:250], dedupe_key),
        )
        _set_lifecycle_status(event_id, 'notify_failed')
    else:
        backoff = min(WA_BACKOFF_BASE * (2 ** (attempt_count - 1)), WA_BACKOFF_CAP)
        cur.execute(
            f"""UPDATE whatsapp_delivery_log
                  SET status='failed', msg91_request_id=%s, last_error=%s,
                      locked_at=NULL, next_attempt_at=(NOW() + INTERVAL {backoff} SECOND)
                WHERE dedupe_key=%s""",
            (result.get("request_id", ""), result.get("error", "")[:250], dedupe_key),
        )
    db.commit()
    cur.close()
    final = 'sent' if result.get("ok") else ('dead' if attempt_count >= WA_MAX_ATTEMPTS else 'failed')
    _audit('whatsapp_result', event_id=event_id, whatsapp_status=final,
           detail=(result.get("request_id", "") or result.get("error", ""))[:255])


def _attempt_whatsapp_job(app, dedupe_key):
    """Claim a due job and attempt one MSG91 send. Idempotent and crash-safe."""
    with app.app_context():
        try:
            job = _claim_due_whatsapp_job(dedupe_key)
            if not job:
                return
            from .notifications import send_whatsapp_tracked
            components = {
                "body_1": {"type": "text", "value": job["body_name"] or "Customer"},
                "body_2": {"type": "text", "value": str(job["ticket_ref"])},
                "body_3": {"type": "text", "value": job["site_host"]},
            }
            result = send_whatsapp_tracked(job["recipient"], job["template_name"], components)
            _finalize_whatsapp_job(dedupe_key, job["event_id"], result, job["attempt_count"])
            app.logger.info(
                f"[KET-EVENT] whatsapp attempt key={dedupe_key} n={job['attempt_count']} "
                f"ok={result.get('ok')} rid={result.get('request_id') or '-'}"
            )
        except Exception as e:  # noqa: BLE001 - worker must never crash the process
            app.logger.error(f"[KET-EVENT] whatsapp attempt failed key={dedupe_key}: {e}")


def _scan_whatsapp_outbox(app):
    """Resume every due/stuck job. Restart-safe: pending work survives crashes."""
    with app.app_context():
        keys = []
        try:
            from .db import get_db
            db = get_db()
            cur = db.cursor()
            _ensure_ket_schema(cur)
            cur.execute(
                f"""SELECT dedupe_key FROM whatsapp_delivery_log
                     WHERE attempt_count < {WA_MAX_ATTEMPTS}
                       AND (
                             ((status='pending' OR status='failed')
                                AND (next_attempt_at IS NULL OR next_attempt_at<=NOW()))
                             OR (status='sending'
                                AND locked_at < (NOW() - INTERVAL {WA_LOCK_TIMEOUT} SECOND))
                           )
                     LIMIT 100"""
            )
            keys = [r["dedupe_key"] for r in cur.fetchall()]
            cur.close()
        except Exception as e:  # noqa: BLE001
            app.logger.error(f"[KET-EVENT] outbox scan failed: {e}")
            return
    for key in keys:
        _attempt_whatsapp_job(app, key)


def start_whatsapp_outbox_worker(app):
    """Start the background outbox loop once per process (restart durability)."""
    def _loop():
        while True:
            try:
                _scan_whatsapp_outbox(app)
            except Exception as e:  # noqa: BLE001
                app.logger.error(f"[KET-EVENT] outbox loop error: {e}")
            time.sleep(WA_SCAN_INTERVAL)
    threading.Thread(target=_loop, daemon=True, name="wa-outbox").start()


def _store_delivery_event(msg91_request_id, status, failure_reason, provider_ts):
    """Persist an MSG91 delivery-status callback + fold it into the outbox row."""
    from .db import get_db
    db = get_db()
    cur = db.cursor()
    _ensure_ket_schema(cur)
    cur.execute(
        """SELECT event_id, ticket_ref, recipient, template_name
             FROM whatsapp_delivery_log WHERE msg91_request_id=%s""",
        (msg91_request_id,),
    )
    row = cur.fetchone()
    event_id = row["event_id"] if row else ''
    ticket_ref = row["ticket_ref"] if row else ''
    recipient = row["recipient"] if row else ''
    template_name = row["template_name"] if row else ''
    cur.execute(
        """INSERT INTO msg91_delivery_events
             (msg91_request_id, event_id, ticket_ref, recipient, template_name,
              status, failure_reason, provider_ts)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (msg91_request_id, event_id, ticket_ref, recipient, template_name,
         status, failure_reason, provider_ts),
    )
    if row and status in ('delivered', 'read', 'failed'):
        cur.execute(
            """UPDATE whatsapp_delivery_log
                 SET status=%s,
                     delivered_at=CASE WHEN %s IN ('delivered','read') THEN NOW() ELSE delivered_at END,
                     last_error=CASE WHEN %s='failed' THEN %s ELSE last_error END
               WHERE msg91_request_id=%s""",
            (status, status, status, failure_reason, msg91_request_id),
        )
    db.commit()
    cur.close()
    return bool(row)


@bp.route('/support/ticket_event', methods=['POST'])
def ket_ticket_event():
    """Inbound KET ticket-lifecycle webhook (schema v1: resolved / reopened).

    KET is the system of record for the lifecycle; when an agent resolves/reopens
    a ticket KET calls this endpoint so Optiwar sends the WhatsApp update from its
    own MSG91 number (Optiwar owns WhatsApp; KET owns only the support email).

    Auth: HMAC-SHA256 over "<X-KET-Timestamp>:<raw_body>" with OPTIWAR_WEBHOOK_SECRET.
    JSON: {version?, event, event_id, ticket_ref, ticket_id?, request_id?, name, phone, email}

    Flow: verify signature -> validate schema -> durably store lifecycle event +
    (if phone) enqueue a durable WhatsApp job -> ack -> a restart-safe background
    worker performs the MSG91 send with retries.
    Codes: 401 bad signature; 400 invalid schema; 413 payload too large;
    200 accepted/duplicate/ignored; 503 when the event/job could not be durably
    stored (KET should retry).
    """
    from flask import jsonify, g
    raw_body = request.get_data(as_text=True)
    if len(raw_body.encode('utf-8', 'ignore')) > KET_MAX_BODY_BYTES:
        current_app.logger.warning("[KET-EVENT] rejected: payload too large")
        _audit('payload_too_large', detail='>64KB')
        return jsonify({"error": "payload too large"}), 413
    if not _verify_ket_signature(
        raw_body,
        request.headers.get('X-KET-Timestamp'),
        request.headers.get('X-KET-Signature'),
    ):
        current_app.logger.warning("[KET-EVENT] rejected: invalid signature")
        _audit('hmac_fail', detail='invalid or stale signature')
        return jsonify({"error": "invalid signature"}), 401

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        _audit('schema_fail', sig_verified=True, detail='invalid json body')
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
        _audit('ignored', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True)
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
        _audit('store_error', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True)
        return jsonify({"error": "temporary storage failure"}), 503
    if outcome == 'duplicate':
        current_app.logger.info(f"[KET-EVENT] duplicate event_id={event_id} event={event} ticket_ref={ticket_ref}")
        _audit('duplicate', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True)
        return jsonify({"status": "duplicate", "event": event, "ticket_ref": ticket_ref})

    app_obj = current_app._get_current_object()

    # Optiwar-owned chat-session side-effects (Option A close / Option B reopen).
    # Best-effort and idempotent: a mapping miss is an operational alert, never a
    # transport failure -- the lifecycle event is already durably stored.
    try:
        _process_session_lifecycle(event, ticket_id, ticket_ref, request_id, event_id)
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-MAP] session lifecycle error event_id={event_id}: {e}")

    if not phone:
        _set_lifecycle_status(event_id, 'skipped_no_phone')
        _audit('accepted', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True,
               whatsapp_status='skipped_no_phone')
        return jsonify({"status": "accepted", "event": event, "ticket_ref": ticket_ref,
                        "event_id": event_id, "whatsapp": "skipped_no_phone"}), 200

    # Respect customer notification preferences (Optiwar-owned WhatsApp policy).
    customer_key = (email or phone).lower()
    if not _whatsapp_pref_allowed(customer_key):
        _set_lifecycle_status(event_id, 'skipped_pref')
        _audit('accepted', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True,
               whatsapp_status='skipped_pref')
        return jsonify({"status": "accepted", "event": event, "ticket_ref": ticket_ref,
                        "event_id": event_id, "whatsapp": "skipped_pref"}), 200

    # Durably enqueue the WhatsApp job BEFORE acking so a restart cannot lose it.
    recipient = phone.replace('+', '').replace(' ', '').replace('-', '')
    dedupe_key = f"ket:{event_id}:whatsapp"
    try:
        _enqueue_whatsapp_job(dedupe_key, event_id, ticket_ref,
                              _KET_EVENT_TEMPLATES[event], recipient, name, request.host)
    except Exception as e:  # noqa: BLE001 - could not persist the job
        current_app.logger.error(f"[KET-EVENT] enqueue failed key={dedupe_key}: {e}")
        _audit('store_error', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
               event_id=event_id, request_id=request_id, sig_verified=True,
               detail='enqueue failed')
        return jsonify({"error": "temporary storage failure"}), 503

    _audit('accepted', event=event, ticket_ref=ticket_ref, ticket_id=ticket_id,
           event_id=event_id, request_id=request_id, sig_verified=True,
           whatsapp_status='enqueued')

    # Kick an immediate attempt for low latency; the outbox worker is the
    # restart-safe guarantee if this process dies before/while sending.
    threading.Thread(target=_attempt_whatsapp_job, args=(app_obj, dedupe_key), daemon=True).start()

    return jsonify({"status": "accepted", "event": event, "ticket_ref": ticket_ref, "event_id": event_id}), 200


@bp.route('/support/msg91_delivery_event', methods=['POST'])
def msg91_delivery_event():
    """MSG91 delivery-status callback (sent/delivered/read/failed).

    Optional shared-token auth via MSG91_DELIVERY_TOKEN (header X-MSG91-Token or
    ?token=). Only known msg91_request_id values are folded into the outbox row;
    every callback is stored in msg91_delivery_events for the audit trail.
    """
    from flask import jsonify
    token = current_app.config.get('MSG91_DELIVERY_TOKEN', '')
    if token:
        supplied = request.headers.get('X-MSG91-Token') or request.args.get('token', '')
        if not hmac.compare_digest(str(supplied), str(token)):
            return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "invalid json body"}), 400

    msg91_request_id = str(
        data.get('request_id') or data.get('requestId')
        or data.get('message_id') or ''
    ).strip()
    status = (
        data.get('status') or data.get('event') or data.get('eventName') or ''
    ).strip().lower()
    failure_reason = str(data.get('failure_reason') or data.get('reason') or '')[:250]
    provider_ts = str(
        data.get('timestamp') or data.get('provider_ts')
        or data.get('statusUpdatedAt') or data.get('ts') or ''
    )[:64]
    if not msg91_request_id or not status:
        return jsonify({"error": "request_id and status required"}), 400

    try:
        matched = _store_delivery_event(msg91_request_id, status, failure_reason, provider_ts)
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-EVENT] delivery event store failed rid={msg91_request_id}: {e}")
        return jsonify({"error": "temporary storage failure"}), 503

    current_app.logger.info(f"[KET-EVENT] delivery status rid={msg91_request_id} status={status} matched={matched}")
    _audit('delivery_status', whatsapp_status=status, detail=f"rid={msg91_request_id} matched={matched}")
    return jsonify({"status": "recorded", "matched": matched}), 200


# ---------------------------------------------------------------------------
# Phase C — customer-visible timeline, notification preferences, and the
# internal monitoring dashboard. All additive; the frozen v1 webhook contract,
# schema, and lifecycle events are untouched.
# ---------------------------------------------------------------------------
_TIMELINE_LABELS = {
    'created': 'Ticket created',
    'resolved': 'Resolved',
    'reopened': 'Reopened',
    'waiting_customer': 'Waiting for you',
    'closed': 'Closed',
}


def _fetch_timeline(ticket_ref):
    """Ordered lifecycle rows for a ticket, oldest first (customer timeline)."""
    from .db import get_db
    db = get_db()
    cur = db.cursor()
    _ensure_ket_schema(cur)
    cur.execute(
        """SELECT event, processing_status, received_at, email
             FROM ket_lifecycle_events WHERE ticket_ref=%s ORDER BY received_at ASC""",
        (ticket_ref,),
    )
    rows = cur.fetchall()
    cur.close()
    return rows


@bp.route('/support/timeline/<ticket_ref>', methods=['GET'])
def support_timeline(ticket_ref):
    """Customer-visible ticket timeline so a customer need not log into KET.

    Light ownership check: the caller must supply ?email= matching the email KET
    recorded for the ticket (case-insensitive) to prevent ticket_ref enumeration.
    """
    from flask import jsonify
    supplied_email = (request.args.get('email') or '').strip().lower()
    try:
        rows = _fetch_timeline(ticket_ref)
    except Exception as e:  # noqa: BLE001 - never 500 a customer page
        current_app.logger.error(f"[TIMELINE] fetch failed ticket={ticket_ref}: {e}")
        rows = []
    known_emails = {(r["email"] or '').lower() for r in rows if r["email"]}
    if not rows or (known_emails and supplied_email not in known_emails):
        # Do not disclose whether the ticket exists.
        if request.args.get('format') == 'json':
            return jsonify({"error": "not found"}), 404
        return render_template('support_timeline.html', ticket_ref=ticket_ref,
                               steps=None, not_found=True), 404
    steps = [{
        "event": r["event"],
        "label": _TIMELINE_LABELS.get(r["event"], r["event"].replace('_', ' ').title()),
        "status": r["processing_status"],
        "at": r["received_at"].strftime('%Y-%m-%d %H:%M UTC') if r["received_at"] else '',
    } for r in rows]
    if request.args.get('format') == 'json':
        return jsonify({"ticket_ref": ticket_ref, "steps": steps})
    return render_template('support_timeline.html', ticket_ref=ticket_ref,
                           steps=steps, not_found=False)


@bp.route('/support/preferences', methods=['GET', 'POST'])
def support_preferences():
    """Read/update a customer's notification channel preferences.

    Keyed on the customer's email (or phone). GET returns current effective prefs
    (defaults ON for email/WhatsApp). POST upserts the provided boolean flags.
    """
    from flask import jsonify
    from .db import get_db
    if request.method == 'POST':
        data = request.get_json(silent=True) or request.form
        customer_key = (data.get('email') or data.get('phone') or '').strip().lower()
        if not customer_key:
            return jsonify({"error": "email or phone required"}), 400

        def _flag(name, default):
            v = data.get(name)
            if v is None:
                return default
            return 1 if str(v).lower() in ('1', 'true', 'yes', 'on') else 0

        email_e = _flag('email_enabled', 1)
        wa_e = _flag('whatsapp_enabled', 1)
        sms_e = _flag('sms_enabled', 0)
        push_e = _flag('push_enabled', 0)
        try:
            db = get_db()
            cur = db.cursor()
            _ensure_ket_schema(cur)
            cur.execute(
                """INSERT INTO notification_preferences
                     (customer_key, email_enabled, whatsapp_enabled, sms_enabled, push_enabled)
                   VALUES (%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE email_enabled=VALUES(email_enabled),
                     whatsapp_enabled=VALUES(whatsapp_enabled),
                     sms_enabled=VALUES(sms_enabled), push_enabled=VALUES(push_enabled)""",
                (customer_key, email_e, wa_e, sms_e, push_e),
            )
            db.commit()
            cur.close()
        except Exception as e:  # noqa: BLE001
            current_app.logger.error(f"[KET-PREF] upsert failed key={customer_key}: {e}")
            return jsonify({"error": "temporary storage failure"}), 503
        return jsonify({"status": "saved", "customer_key": customer_key,
                        "email_enabled": bool(email_e), "whatsapp_enabled": bool(wa_e),
                        "sms_enabled": bool(sms_e), "push_enabled": bool(push_e)})

    customer_key = (request.args.get('email') or request.args.get('phone') or '').strip().lower()
    if not customer_key:
        return jsonify({"error": "email or phone required"}), 400
    try:
        db = get_db()
        cur = db.cursor()
        _ensure_ket_schema(cur)
        cur.execute(
            """SELECT email_enabled, whatsapp_enabled, sms_enabled, push_enabled
                 FROM notification_preferences WHERE customer_key=%s""",
            (customer_key,),
        )
        row = cur.fetchone()
        cur.close()
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-PREF] read failed key={customer_key}: {e}")
        return jsonify({"error": "temporary storage failure"}), 503
    if not row:
        return jsonify({"customer_key": customer_key, "email_enabled": True,
                        "whatsapp_enabled": True, "sms_enabled": False,
                        "push_enabled": False, "defaults": True})
    return jsonify({"customer_key": customer_key,
                    "email_enabled": bool(row["email_enabled"]),
                    "whatsapp_enabled": bool(row["whatsapp_enabled"]),
                    "sms_enabled": bool(row["sms_enabled"]),
                    "push_enabled": bool(row["push_enabled"]), "defaults": False})


def _monitor_metrics(hours):
    """Aggregate operational counters for the support-integration dashboard."""
    from .db import get_db
    db = get_db()
    cur = db.cursor()
    _ensure_ket_schema(cur)
    since = f"NOW() - INTERVAL {int(hours)} HOUR"
    counts = {}
    cur.execute(
        f"""SELECT kind, COUNT(*) AS n FROM support_event_audit
             WHERE occurred_at >= {since} GROUP BY kind""")
    for r in cur.fetchall():
        counts[r["kind"]] = int(r["n"])
    cur.execute(
        f"""SELECT status, COUNT(*) AS n FROM whatsapp_delivery_log
             WHERE created_at >= {since} GROUP BY status""")
    wa = {r["status"]: int(r["n"]) for r in cur.fetchall()}
    cur.execute(
        """SELECT COUNT(*) AS n FROM ket_lifecycle_events
             WHERE event='reopened'
               AND event_id NOT IN (SELECT event_id FROM ket_lifecycle_events WHERE event='resolved')""")
    cur.close()
    return {
        "window_hours": int(hours),
        "webhook_accepted": counts.get('accepted', 0),
        "duplicates": counts.get('duplicate', 0),
        "ignored_future": counts.get('ignored', 0),
        "hmac_failures": counts.get('hmac_fail', 0),
        "schema_failures": counts.get('schema_fail', 0),
        "payload_too_large": counts.get('payload_too_large', 0),
        "store_errors": counts.get('store_error', 0),
        "whatsapp_by_status": wa,
        "whatsapp_failed": wa.get('failed', 0),
        "whatsapp_dead": wa.get('dead', 0),
        "whatsapp_sent": wa.get('sent', 0) + wa.get('delivered', 0) + wa.get('read', 0),
        "sessions_closed": counts.get('session_close', 0),
        "sessions_reopened": counts.get('session_reopen', 0),
        "session_not_found": counts.get('session_not_found', 0),
        "ref_fallback_join": counts.get('ref_fallback_join', 0),
    }


@bp.route('/support/monitor', methods=['GET'])
def support_monitor():
    """Internal support-integration monitoring dashboard (admin only)."""
    from flask import jsonify
    from .ops import _require_ops_auth
    if not _require_ops_auth():
        return jsonify({"error": "unauthorized"}), 401
    try:
        hours = int(request.args.get('hours', 24))
    except (TypeError, ValueError):
        hours = 24
    hours = max(1, min(hours, 720))
    try:
        metrics = _monitor_metrics(hours)
    except Exception as e:  # noqa: BLE001
        current_app.logger.error(f"[KET-MONITOR] failed: {e}")
        return jsonify({"error": "metrics unavailable"}), 500
    if request.args.get('format') == 'json':
        return jsonify(metrics)
    return render_template('support_monitor.html', m=metrics)
