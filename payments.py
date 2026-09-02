import os
import requests
import json
from flask import current_app, url_for
from paytmchecksum import PaytmChecksum
from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes


def verify_payment_status(order_id):
    paytmParams = dict()
    paytmParams["body"] = {
            "mid": current_app.config['PAYTM_MID'],
            "orderId": order_id,
    }

    checksum = PaytmChecksum.generateSignature(json.dumps(paytmParams["body"]), current_app.config['PAYTM_MERCHANT_KEY'])

    paytmParams["head"] = {
            "signature": checksum
    }

    post_data = json.dumps(paytmParams)
    #print(post_data)
    url = "https://secure.paytmpayments.com/v3/order/status"


    try:
        response = requests.post(url, data = post_data, headers = {"Content-type": "application/json"})
        #print(response)
        response_json = response.json()
        #print(f" {order_id}")
        #print(f" {post_data} ")
        #print(f" {response_json}")
        return response_json


    except Exception as e:
        print(f"  {e}")
        return {"error": str(e)}

def initiate_payment(order_id, total_amount, customer_id, callbackurl):
    """Initiates a payment transaction."""
    paytmParams = dict()
    paytmParams ["body"]= {
            "requestType": "Payment",
            "mid": current_app.config['PAYTM_MID'],
            "websiteName": "DEFAULT",
            "orderId": order_id,
            "callbackUrl": url_for('main.payment_callback', _external=True),
            "txnAmount": {
                "value": str(total_amount),
                "currency": "INR",
            },
            "userInfo": {
                "custId": str(customer_id),
            },
        }

    # Generate the checksum using your key
    checksum = PaytmChecksum.generateSignature(json.dumps(paytmParams["body"]), current_app.config['PAYTM_MERCHANT_KEY'])
    paytmParams["head"] = {
        "signature": checksum
    }

    post_data = json.dumps(paytmParams)

    # URL for staging or production
    url = f"https://secure.paytmpayments.com/theia/api/v1/initiateTransaction?mid={current_app.config['PAYTM_MID']}&orderId={order_id}"


    response = requests.post(url, data=post_data, headers={"Content-type": "application/json"})
    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error initiating payment: {response.status_code}")
        return None


# ============================================================
# Razorpay Integration (optiwar.com — EUR payments)
# ============================================================
import razorpay
import hmac
import hashlib

from .razorpay_events import verify_webhook_signature

def get_razorpay_client():
    """Get Razorpay client instance."""
    return razorpay.Client(auth=(
        current_app.config['RAZORPAY_KEY_ID'],
        current_app.config['RAZORPAY_KEY_SECRET']
    ))

def create_razorpay_order(order_id, amount_eur, currency='EUR'):
    """Create a Razorpay order for EUR payments.
    amount_eur: float amount in EUR (e.g. 35.99)
    Returns: Razorpay order dict with 'id', 'amount', 'currency' etc.
    """
    client = get_razorpay_client()
    # Razorpay expects amount in smallest currency unit (cents for EUR)
    amount_cents = int(round(amount_eur * 100))
    data = {
        'amount': amount_cents,
        'currency': currency,
        'receipt': str(order_id),
        'payment_capture': 1  # Auto-capture payment
    }
    try:
        order = client.order.create(data=data)
        current_app.logger.info(f"Razorpay order created: {order['id']} for {currency} {amount_eur}")
        return order
    except Exception as e:
        current_app.logger.error(f"Razorpay order creation failed: {e}")
        raise

def verify_razorpay_payment(razorpay_order_id, razorpay_payment_id, razorpay_signature):
    """Verify Razorpay payment signature."""
    key_secret = current_app.config['RAZORPAY_KEY_SECRET']
    msg = razorpay_order_id + '|' + razorpay_payment_id
    generated_signature = hmac.new(
        key_secret.encode('utf-8'),
        msg.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return generated_signature == razorpay_signature

def verify_razorpay_payment_link(order_id, args, key_secret=None):
    """Whether a payment-link return proves *this* order was paid.

    The signed payload is
    payment_link_id|payment_link_reference_id|payment_link_status|razorpay_payment_id

    The reference has to be the order asked for and the status has to be paid, so
    a signature Razorpay issued for one order cannot open another one, and an
    abandoned or cancelled link authorises nothing.
    """
    if args.get('razorpay_payment_link_reference_id') != order_id:
        return False
    if args.get('razorpay_payment_link_status') != 'paid':
        return False
    link_id = args.get('razorpay_payment_link_id', '')
    reference_id = args.get('razorpay_payment_link_reference_id', '')
    status = args.get('razorpay_payment_link_status', '')
    payment_id = args.get('razorpay_payment_id', '')
    signature = args.get('razorpay_signature', '')
    if not (link_id and reference_id and status and payment_id and signature):
        return False
    if key_secret is None:
        key_secret = (current_app.config.get('RAZORPAY_KEY_SECRET')
                      or os.environ.get('RAZORPAY_KEY_SECRET', ''))
    if not key_secret:
        return False
    msg = '%s|%s|%s|%s' % (link_id, reference_id, status, payment_id)
    expected = hmac.new(
        key_secret.encode('utf-8'),
        msg.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def verify_razorpay_webhook(raw_body, signature):
    """Verify a Razorpay webhook against the raw request body.

    Falls back to the environment when the app config has no entry, so the
    webhook can be deployed without also deploying the factory that reads it.
    Fails closed either way: with no secret anywhere, every delivery is
    rejected.
    """
    secret = (current_app.config.get('RAZORPAY_WEBHOOK_SECRET')
              or os.environ.get('RAZORPAY_WEBHOOK_SECRET', ''))
    return verify_webhook_signature(raw_body, signature, secret)


def fetch_razorpay_payment(payment_id):
    """Fetch payment details from Razorpay."""
    client = get_razorpay_client()
    try:
        return client.payment.fetch(payment_id)
    except Exception as e:
        current_app.logger.error(f"Razorpay payment fetch failed: {e}")
        return None
