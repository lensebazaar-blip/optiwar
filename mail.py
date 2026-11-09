import smtplib
import time
from email.message import EmailMessage
from flask import current_app, request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.message import EmailMessage
from .db import get_db


def format_cart_details(cart):
    """Format cart details in HTML"""
    cart_items = ""
    for item in cart:
        total = item.get('ATC_total') or item.get('server_total_price') or item.get('ATC_WCL') or 0
        prescription = ""
        prescription_parts = []

        category = item.get('product_category', '')

        if category == 'Contact Lenses':
            if item.get('right_pwr'):
                prescription_parts.append(f"Right PWR: {item['right_pwr']}")
  
            if item.get('left_pwr'):
                prescription_parts.append(f"Left PWR: {item['left_pwr']}")
            if item.get('right_lens_color'):
                prescription_parts.append(f"Right Color: {item['right_lens_color']} {item['right_qty']} ")
            if item.get('left_lens_color'):
                prescription_parts.append(f"Left Color: {item['left_lens_color']} {item['left_qty']} ")
            if item.get('order_quantity'):
                prescription_parts.append(f"Total: {item['order_quantity']} boxes")
  

        elif category == 'Spectacles Frame':
            if item.get('recommendations'):
                prescription_parts.append(f"Lens to be fitted: {item['recommendations']}")
            if item.get('addon_3_name'):
                prescription_parts.append(f"{item['addon_3_name']} : Rs. {item['addon_3_price']}")
            if item.get('addon_1_name'):
                prescription_parts.append(f"{item['addon_1_name']} : Rs. {item['addon_1_price']}")
            if item.get('addon_2_name'):
                prescription_parts.append(f"{item['addon_2_name']} : Rs. {item['addon_2_price']}")


            if item.get('right_eye'):
                prescription_parts.append(f"Right Eye: {item['right_eye']}")
            if item.get('left_eye'):
                prescription_parts.append(f"Left Eye: {item['left_eye']}")
            if item.get('product_code'):
                prescription_parts.append(f"Product Code: {item['product_code']}")
            if item.get('order_quantity'):
                prescription_parts.append(f"Quantity: {item['order_quantity']}")

        if prescription_parts:
            prescription = "<br>".join(prescription_parts)

        cart_items += f"""
        <tr>
            <td style="padding: 10px; border: 1px solid #ddd;">{item.get('product_name', '')}</td>
            <td style="padding: 10px; border: 1px solid #ddd;">{item.get('order_quantity', 1)}</td>
            <td style="padding: 10px; border: 1px solid #ddd;">Rs {item.get('product_special_price', 0)}</td>
            <td style="padding: 10px; border: 1px solid #ddd;">Rs {total}</td>
            <td style="padding: 10px; border: 1px solid #ddd;">{prescription}</td>
        </tr>
        """
    return cart_items



'''
def format_cart_details(cart):
    """ Format cart details """
    cart_items = ""
    for item in cart:
        total = item.get('ATC_total', item.get('server_total_price'))
        cart_items += f"""
        <tr>
            <td style="padding: 10px; border: 1px solid #ddd;">{item['product_name']} </td>
            <td style="padding: 10px; border: 1px solid #ddd;">{item['order_quantity']} </td>
            <td style="padding: 10px; border: 1px solid #ddd;">{item['product_special_price']} </td>
            <td style="padding: 10px; border: 1px solid #ddd;"> {country} </td>
        </tr>
        """
    return cart_items
'''

def send_order_confirmation(to_email, to_cc, order_id, customer_name, cart):
    """ Sends an order confirmation email in HTML format """
    cart_details_html = format_cart_details(cart)

    # Safe grand total computation
    try:
        grand_total = sum(
            item.get('ATC_total') or item.get('server_total_price') or item.get('ATC_WCL') or 0
            for item in cart
        )
    except Exception as e:
        print(f"❌ Error calculating grand total in email: {e}")
        grand_total = 0

    html_body = f"""
    <body style="background-color:#f0f0f0; font-family: Arial, sans-serif;">
        <table align="center" border="0" cellpadding="0" cellspacing="0"
               width="600" bgcolor="white" style="border:2px solid #cccccc; margin: 20px auto;">
            <tbody>
                <tr>
                    <td align="center" style="background-color: #4cb96b; padding: 20px;">
                        <p style="color:white; font-size: 24px; font-weight:bold;">
                            Order Confirmation - Order ID: {order_id}
                        </p>
                    </td>
                </tr>
                <tr>
                    <td align="center" style="padding: 20px;">
                        <p style="font-size: 18px; color:#333333;">
                            Hello {customer_name},<br> Thank you for your order! Here are your order details:
                        </p>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 20px;">
                        <table width="100%" cellpadding="10" cellspacing="0" style="border-collapse: collapse; text-align: left;">
                            <thead style="background-color: #f8f8f8;">
                                <tr>
                                    <th style="border: 1px solid #ddd; padding: 10px;">Product Name</th>
                                    <th style="border: 1px solid #ddd; padding: 10px;">Quantity</th>
                                    <th style="border: 1px solid #ddd; padding: 10px;">Unit Price</th>
                                    <th style="border: 1px solid #ddd; padding: 10px;">Total Price</th>
                                    <th style="border: 1px solid #ddd; padding: 10px;">Detailed info</th>
                                </tr>
                            </thead>
                            <tbody>
                                {cart_details_html}
                            </tbody>
                        </table>
                    </td>
                </tr>
                <tr>
                    <td style="padding: 20px; text-align: right;">
                        <p style="font-size: 18px; font-weight: bold; color: #333;">
                            Grand Total: Rs {grand_total}
                        </p>
                    </td>
                </tr>
                <tr>
                    <td align="center" style="padding: 20px;">
                        <p style="font-size: 16px; color:#333333;">
                            Do us a favour and mark this email as safe, as we will not spam your mailbox :)<br>
                            If you have any questions, just reply to this email.<br>
                            <a href="https://optiwar.com" style="color: #4cb96b; text-decoration: none; font-weight: bold;">Visit Optiwar</a>
                        </p>
                    </td>
                </tr>
            </tbody>
        </table>
    </body>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"Order confirmation - Order {order_id}"
    msg['From'] = current_app.config['MAIL_USERNAME']
    msg['To'] = to_email
    msg['Cc'] = to_cc
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP(current_app.config['MAIL_SERVER'], current_app.config['MAIL_PORT']) as server:
            server.starttls()
            server.login(current_app.config['MAIL_USERNAME'], current_app.config['MAIL_PASSWORD'])
            server.send_message(msg)
            print(f"✅ Email sent to {to_email} for Order ID {order_id}")
    except Exception as e:
        print(f"❌ Error sending email: {e}")


def send_otp_email(email, otp):
    """
    Sends an OTP to the provided email address using smtplib and EmailMessage.
    :param email: The recipient's email address
    :param otp: The 4-digit OTP to send
    :return: None
    """
    try:
        email_content = f"""
        Dear User,

        Your OTP for verifying your order is {otp}. Please enter this OTP to proceed.

        Thank you,
        Optiwar
        """

        msg = EmailMessage()
        msg['Subject'] = "Your OTP from Optiwar"
        msg['From'] = current_app.config['MAIL_USERNAME']
        msg['To'] = email
        msg.set_content(email_content)

        with smtplib.SMTP(current_app.config['MAIL_SERVER'], current_app.config['MAIL_PORT']) as server:
            server.starttls()
            server.login(current_app.config['MAIL_USERNAME'], current_app.config['MAIL_PASSWORD'])
            server.send_message(msg)
            print(f"OTP email sent to {email}")

    except Exception as e:
        print(f"Failed to send OTP email: {e}")


def create_ticket_in_db(name, email, subject, message):
    """
    Inserts tickets into DB table tickets
    :param name: Name of the sender
    :param email: Email of the sender
    :param subject: Subject of the ticket
    :param message: Message of the sender
    """
    try:
       db = get_db()
       cursor = db.cursor()
       ip_address = request.remote_addr
       cursor.execute("""
          insert into tickets (name, email, subject, message, ip_address) values (%s, %s, %s, %s, %s)
       """, (name, email, subject, message, ip_address))

       cursor.execute("select LAST_INSERT_ID() as id")
       ticket_id_row = cursor.fetchone()
       ticket_id = ticket_id_row['id'] if isinstance (ticket_id_row, dict) else ticket_id_row[0]
       db.commit()
       return ticket_id

    except Exception as e:
        current_app.logger.error(f" Failed to insert ticket into db: {e}",  exc_info=True)
        db.rollback()
        raise RuntimeError("Database Error while creating ticket") from e

def send_contact_email(name, email, phone, subject, message, ticket_id):
    """
    Sends an internal admin notification for a new support ticket.

    This is a best-effort, out-of-transaction notification: it NEVER raises and
    never fails ticket creation or the customer-facing confirmation. A transient
    SMTP failure is retried a bounded number of times and, if still failing, is
    logged for follow-up rather than propagated to the request.

    It does run inside the request, so "never blocks" is a bound and not an
    absolute: MAIL_TIMEOUT (5s) x 3 attempts + 3s of backoff is the worst case a
    customer can wait on an unreachable mail host. Moving the notification off
    the request path is the real fix and is not this change.

    :param name: Name of the user submitting the form
    :param email: Email of the user submitting the form
    :param subject: Subject of the user's query
    :param message: Detailed message from the user
    :param ticket_id: Generated ticket ID for the query
    :return: True if the notification was sent, False otherwise
    """
    if not current_app.config.get('APP_TICKET_EMAIL_ENABLED', False):
        current_app.logger.info(
            f"App-side ticket email disabled; skipping ticket_id={ticket_id}"
        )
        return False

    admin_recipient = current_app.config.get('ADMIN_NOTIFY_EMAIL', 'admin@optiwar.com')
    email_content = f"""
    Dear Admin,

    A new support request form submission has been received:

    Optiwar Support Ticket ID : {ticket_id}
    Name: {name}
    Email: {email}
    Subject: {subject}
    Phone : {phone}
    Message:
    {message}

    Please respond to this query at your earliest convenience.

    Thank you,
    Optiwar System
    """

    msg = EmailMessage()
    # Prefix distinguishes Optiwar-app alerts from KET staff notifications
    # while both run in parallel during the notification-ownership migration.
    msg['Subject'] = f"[Optiwar App] Support Ticket ID: {ticket_id} || {subject}"
    msg['From'] = f"Optiwar Support <{current_app.config['MAIL_USERNAME']}>"
    msg['To'] = admin_recipient
    msg['Reply-To'] = email
    msg.set_content(email_content)

    # This runs inside the request, before the customer sees their confirmation,
    # so the retries need a ceiling in seconds and not only in attempts: an
    # unreachable mail host would otherwise block on the socket default, which is
    # no timeout at all. 3 attempts x timeout + 1s + 2s of backoff.
    timeout = float(current_app.config.get('MAIL_TIMEOUT', 5))
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with smtplib.SMTP(current_app.config['MAIL_SERVER'],
                              current_app.config['MAIL_PORT'],
                              timeout=timeout) as server:
                server.starttls()
                server.login(current_app.config['MAIL_USERNAME'], current_app.config['MAIL_PASSWORD'])
                server.send_message(msg)
            current_app.logger.info(
                f"Admin ticket notification sent for ticket_id={ticket_id} (attempt {attempt})"
            )
            return True
        except Exception as e:
            current_app.logger.warning(
                f"Admin ticket notification attempt {attempt}/{max_attempts} "
                f"failed for ticket_id={ticket_id}: {e}"
            )
            if attempt < max_attempts:
                time.sleep(min(attempt, 3))

    current_app.logger.error(
        f"Admin ticket notification permanently failed for ticket_id={ticket_id} "
        f"after {max_attempts} attempts; ticket + customer flow unaffected"
    )
    return False




def send_review_request(to_email, customer_name, product_name, product_code, product_image, review_url, site_domain='optiwar.com'):
    """Send a post-purchase review request email."""
    img_url = f"https://{site_domain}/static/{product_image.split(',')[0].strip()}" if product_image else f"https://{site_domain}/static/images/logo.png"

    html_body = f"""
    <body style="background-color:#f5f5f5;font-family:Arial,sans-serif;margin:0;padding:0;">
      <table align="center" border="0" cellpadding="0" cellspacing="0" width="560" style="background:#fff;border-radius:12px;margin:24px auto;overflow:hidden;">
        <tr><td style="background:linear-gradient(135deg,#6366f1,#8b5cf6);padding:28px;text-align:center;">
          <p style="color:#fff;font-size:22px;font-weight:700;margin:0;">How are your new frames?</p>
        </td></tr>
        <tr><td style="padding:28px;text-align:center;">
          <img src="{img_url}" alt="{product_name}" style="max-width:200px;border-radius:12px;margin-bottom:16px;" />
          <p style="font-size:16px;color:#334155;margin:8px 0;">Hi {customer_name},</p>
          <p style="font-size:14px;color:#64748b;line-height:1.6;">
            We hope you&rsquo;re enjoying your <strong>{product_name} {product_code}</strong>!<br>
            Your feedback helps other customers find the perfect frames.
          </p>
          <a href="{review_url}" style="display:inline-block;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;margin:20px 0;">
            &#9733; Write a Quick Review
          </a>
          <p style="font-size:12px;color:#94a3b8;margin-top:16px;">
            Takes less than 30 seconds. Just pick a star rating and optionally add a comment.
          </p>
        </td></tr>
        <tr><td style="background:#f8fafc;padding:16px;text-align:center;">
          <p style="font-size:11px;color:#94a3b8;margin:0;">
            &copy; Optiwar &mdash; Factory Direct Eyewear | <a href="https://{site_domain}" style="color:#6366f1;">Visit Store</a>
          </p>
        </td></tr>
      </table>
    </body>
    """

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"How are your {product_name} frames? Share your experience"
    msg['From'] = current_app.config['MAIL_USERNAME']
    msg['To'] = to_email
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP(current_app.config['MAIL_SERVER'], current_app.config['MAIL_PORT']) as server:
            server.starttls()
            server.login(current_app.config['MAIL_USERNAME'], current_app.config['MAIL_PASSWORD'])
            server.send_message(msg)
            print(f"[ReviewRequest] Email sent to {to_email} for {product_name} {product_code}")
            return True
    except Exception as e:
        print(f"[ReviewRequest] Error sending email: {e}")
        return False
