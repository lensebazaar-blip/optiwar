"""Mails for the public forms: trademark / copyright notices and supplier
applications. A module of its own because production's ``mail.py`` is not
in the deploy set (it carries its own reviewed copy); nothing here touches or
depends on it beyond the same MAIL_* configuration.

Each sender opens one SMTP session and sends two messages: the submission to
Optiwar's admin mailboxes (Reply-To the sender) and a courteous confirmation
to the sender (Cc the admins). Best effort with bounded retries; never raises.
"""
import smtplib
import time
from email.message import EmailMessage

from flask import current_app


def send_ip_notice(ref, holder_email, holder_name, text, holder_text=None, recipients=None):
    """Two mails for a trademark / copyright notice, in one SMTP session:
    the notice to Optiwar (To admin@optiwar.com and admin@lensbazaar.com,
    Reply-To the Rights Holder) and a courteous acknowledgement to the Rights
    Holder with the notice copy. Best effort with bounded retries; never
    raises; True when both went."""
    recipients = list(recipients or ("admin@optiwar.com", "admin@lensbazaar.com"))
    sender = f"Optiwar Legal <{current_app.config['MAIL_USERNAME']}>"
    internal = EmailMessage()
    internal['Subject'] = "[Optiwar] Trademark / copyright notice %s" % ref
    internal['From'] = sender
    internal['To'] = ", ".join(recipients)
    internal['Reply-To'] = holder_email
    internal.set_content("A rights holder sent a notice through the website form.\n\n%s\n" % text)
    holder = EmailMessage()
    holder['Subject'] = "Your notice to Optiwar - reference %s" % ref
    holder['From'] = sender
    holder['To'] = holder_email
    holder['Cc'] = ", ".join(recipients)
    holder['Reply-To'] = recipients[0]
    holder.set_content(holder_text or ("Dear %s,\n\nThank you for your notice (reference %s). We will reply in "
                                       "writing; please hold any further action until you hear from us.\n\n%s\n"
                                       % (holder_name, ref, text)))
    timeout = float(current_app.config.get('MAIL_TIMEOUT', 5))
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP(current_app.config['MAIL_SERVER'], current_app.config['MAIL_PORT'],
                              timeout=timeout) as server:
                server.starttls()
                server.login(current_app.config['MAIL_USERNAME'], current_app.config['MAIL_PASSWORD'])
                server.send_message(internal)
                server.send_message(holder)
            current_app.logger.info("IP notice mails sent ref=%s (attempt %d)", ref, attempt)
            return True
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("IP notice mail attempt %d/3 failed ref=%s: %s", attempt, ref, e)
            if attempt < 3:
                time.sleep(min(attempt, 3))
    current_app.logger.error("IP notice mail permanently failed ref=%s", ref)
    return False


def send_supplier_signup(ref, supplier_email, business_name, text, letter, recipients=None):
    """Two mails for a supplier application, in one SMTP session: the application
    to Optiwar's admin mailboxes (Reply-To the supplier) and the signup
    confirmation letter to the supplier (Cc the admins). Never raises."""
    recipients = list(recipients or ("admin@optiwar.com", "admin@lensbazaar.com"))
    sender = f"Optiwar Sourcing <{current_app.config['MAIL_USERNAME']}>"
    internal = EmailMessage()
    internal['Subject'] = "[Optiwar] Supplier application %s - %s" % (ref, business_name)
    internal['From'] = sender
    internal['To'] = ", ".join(recipients)
    internal['Reply-To'] = supplier_email
    internal.set_content("A business applied to supply Optiwar through the website form.\n\n%s\n" % text)
    confirmation = EmailMessage()
    confirmation['Subject'] = "Welcome to Optiwar sourcing - your application %s" % ref
    confirmation['From'] = sender
    confirmation['To'] = supplier_email
    confirmation['Cc'] = ", ".join(recipients)
    confirmation['Reply-To'] = recipients[0]
    confirmation.set_content(letter)
    timeout = float(current_app.config.get('MAIL_TIMEOUT', 5))
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP(current_app.config['MAIL_SERVER'], current_app.config['MAIL_PORT'],
                              timeout=timeout) as server:
                server.starttls()
                server.login(current_app.config['MAIL_USERNAME'], current_app.config['MAIL_PASSWORD'])
                server.send_message(internal)
                server.send_message(confirmation)
            current_app.logger.info("Supplier signup mails sent ref=%s (attempt %d)", ref, attempt)
            return True
        except Exception as e:  # noqa: BLE001
            current_app.logger.warning("Supplier signup mail attempt %d/3 failed ref=%s: %s", attempt, ref, e)
            if attempt < 3:
                time.sleep(min(attempt, 3))
    current_app.logger.error("Supplier signup mail permanently failed ref=%s", ref)
    return False
