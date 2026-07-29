#!/venv/bin/python3
import mysql.connector
import os
import requests
import json
from datetime import datetime, timedelta
from paytmchecksum import PaytmChecksum
import time
import smtplib
from email.message import EmailMessage
from email.utils import formatdate

# ---- Email config ----
SMTP_SERVER = "mail.lensbazaar.com"
SMTP_PORT = 587
SMTP_USERNAME = "admin@lensbazaar.com"
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ADMIN_EMAIL = "admin@lensbazaar.com"

# ---- Paytm API config ----
MID = os.environ.get('PAYTM_MID', '')
MERCHANT_KEY = os.environ.get('PAYTM_MERCHANT_KEY', '')
PAYTM_ENV = "production"

# ---- MySQL DB config ----
db_config = {
    "user": "oslb6",
    "password": os.environ.get('MYSQL_PASSWORD', ''),
    "host": "localhost",
    "port": 3306,
    "database": "optiwar2"
}

# Time range: 72 hours ago until now
now = datetime.now()
seventy_two_hours_ago = now - timedelta(hours=72)

# Paytm endpoint
url = "https://secure.paytmpayments.com/v3/order/status"

def send_email(order_id, full_body, cc_emails):
    msg = EmailMessage()
    msg['Subject'] = f"[ALERT] TXN_SUCCESS for Order ID: {order_id}"
    msg['From'] = SMTP_USERNAME
    msg['To'] = ADMIN_EMAIL
    msg['Cc'] = ', '.join(cc_emails)
    msg['Date'] = formatdate(localtime=True)

    body_text = f"""\
Hello,

✅ Payment for Order ID {order_id} is marked as TXN_SUCCESS and will be processed shortly.
Do not place a duplicate order — wait for your confirmation email.

Below is the full Paytm receipt summary:

{json.dumps(full_body, indent=4)}

Regards,
LensBazaar Robots
"""
    msg.set_content(body_text)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
            print(f"[✔] Email sent for Order ID {order_id}")
    except Exception as e:
        print(f"[✖] Failed to send email for {order_id}: {e}")

try:
    conn = mysql.connector.connect(**db_config)
    cursor = conn.cursor(buffered=True)

    # ---- Your exact SQL with proper datetime params ----
    query = """
    SELECT order_status_name, order_id
    FROM order_status
    WHERE order_status_name = 'Pending'
      AND date_added BETWEEN %s AND %s
    """
    cursor.execute(query, (seventy_two_hours_ago, now))

    if cursor.rowcount == 0:
        print("No recent orders found.")
    else:
        for order_status_name, order_id in cursor:
            print(f"\n🔍 Checking Paytm status for Order ID: {order_id}")

            paytmParams = {
                "body": {
                    "mid": MID,
                    "orderId": str(order_id)
                }
            }
            checksum = PaytmChecksum.generateSignature(
                json.dumps(paytmParams["body"]), MERCHANT_KEY
            )
            paytmParams["head"] = {"signature": checksum}

            try:
                response = requests.post(
                    url,
                    data=json.dumps(paytmParams),
                    headers={"Content-type": "application/json"}
                ).json()

                result_status = response.get("body", {}).get("resultInfo", {}).get("resultStatus", "Unknown")
                print(f"Order ID: {order_id}, Payment Status: {result_status}")

                if result_status == "TXN_SUCCESS":
                    # If email is not available from DB, just send to service@...
                    cc_emails = ["admin@lensbazaar.com"]
                    send_email(order_id, response, cc_emails)

            except Exception as e:
                print(f"[✖] Failed to fetch status for Order ID {order_id}: {e}")

            time.sleep(0.5)

except mysql.connector.Error as err:
    print(f"MySQL Error: {err}")

finally:
    if 'cursor' in locals():
        cursor.close()
    if 'conn' in locals():
        conn.close()

