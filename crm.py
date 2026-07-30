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

bp = Blueprint('crm', __name__)
captcha_generator = CaptchaGenerator()

KET_API_URL = os.environ.get("KET_SUPPORT_URL", "https://support.ket.ltd/new/api/v1/external/messages")


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

        return jsonify({"reply": reply, "done": done, "ticket_data": ticket_data})

    except Exception as e:
        current_app.logger.error(f"[AI-CHAT] Error: {e}", exc_info=True)
        return jsonify({"error": "AI service temporarily unavailable. Please use the manual form."}), 500


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

        if not email or not subject or not description:
            return jsonify({"error": "Missing required fields (email, subject, description)"}), 400

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
        return jsonify({"error": "Failed to create ticket. Please try the manual form."}), 500
