import logging
import random
from flask import Flask, request, render_template, flash, Blueprint, session, url_for, redirect, jsonify
from .db import get_db
from .mail import send_otp_email  # Assuming mail.py has send_otp_email function
from datetime import datetime
from .captcha import CaptchaGenerator


bp = Blueprint('orders', __name__)

logging.basicConfig(level=logging.DEBUG)

# Generate a 4-digit OTP
def generate_otp():
    return random.randint(1000, 9999)



@bp.route('/my_orders', methods=['POST', 'GET'])
def my_order():
    if request.method == 'POST':
        contact = request.form.get('contact')
        otp_input = request.form.get('otp')  # To capture the OTP entered by the user
        logging.debug(f"Received contact: {contact}")
        captcha = request.form.get('captcha')

        if captcha:
             flash(' Our systems cannot verify you are human')
             return render_template('my_orders.html', otp_required=False, submitted=True)

        db = get_db()
        cursor = db.cursor()

        otp_count = 0 # Initiallizing otp_count

        # Check if the contact (email or phone) exists in the database
        cursor.execute('''SELECT customer_id FROM customers WHERE customer_email = %s OR customer_phone = %s''',
                       (contact, contact))
        customer = cursor.fetchone()

        # If the customer does not exist, flash a message and return
        if not customer:
            flash('No orders related to this email ID/phone.')
            return render_template('my_orders.html', otp_required=False, submitted=True)

        # Check how many OTPs have been sent today for this contact
        cursor.execute('''SELECT otp_count FROM otp_requests
                          WHERE contact = %s
                          AND otp_sent_at >= NOW() - INTERVAL 1 DAY''', (contact,))
        otp_record = cursor.fetchone()
        print(type(otp_record))
        # If no record is found, set otp_count to 0; otherwise extract the count from the tuple
        if otp_record is not None:
            otp_count = otp_record['otp_count']  # otp_record is a tuple, extract the first element

        # If more than or equal to 5 OTPs have been sent, show a flash message
        if otp_count >= 5:
            contact_us_url = url_for('crm.create_ticket')
            flash(f'You attempted too many times  and our system believes there is some issue with your order checking. <br> Why dont you <a href="{contact_us_url}" > click here and write us a ticket to </a> help you')
            return render_template('my_orders.html', otp_required=False, submitted=True)

        # If OTP is already sent and the user has entered the OTP, verify the OTP
        if 'otp_sent' in session and session['otp_sent'] and otp_input:
            if str(otp_input) == session.get('otp'):  # Compare input OTP with the session OTP
                logging.debug("OTP verified successfully.")

                # Fetch orders after OTP verification
                cursor.execute('''SELECT o.order_id, os.order_status_name, o.date_created, p.product_name, p.product_special_price, p.product_price, p.product_image, o.order_total, o.order_quantity
                                  FROM orders o
                                  JOIN customers c ON o.customer_id = c.customer_id
                                  JOIN order_status os ON o.order_id = os.order_id
                                  JOIN products p ON p.product_id = o.product_id
                                  WHERE c.customer_email = %s OR c.customer_phone = %s
                                  GROUP BY o.order_id, p.product_image
                                  ORDER BY o.date_created DESC ''',
                               (contact, contact))

                orders = cursor.fetchall()
                logging.debug(f"Fetched orders with statuses: {orders}")

                if orders:
                    session.pop('otp', None)  # Clear OTP from session after successful verification
                    return render_template('my_orders.html', order_found=True, orders=orders, submitted=True)
                else:
                    flash('No orders found for this contact.')
                    return render_template('my_orders.html', order_found=False, submitted=True)

            else:
                flash('Invalid OTP. Please try again.')
                return render_template('my_orders.html', otp_required=True, contact=contact, submitted=True)

        # If the contact exists and OTP needs to be sent
        else:
            # Generate a new OTP and store it in the session
            otp = generate_otp()
            session['otp'] = str(otp)
            session['otp_sent'] = True
            logging.debug(f"Generated OTP: {otp}")

            # Send OTP to the user's email if an email is provided
            if '@' in contact:
                try:
                    send_otp_email(contact, otp)  # Assuming this function exists in mail.py
                    flash('OTP sent to your email. Please check your inbox.')

                    # If no record exists, insert a new one
                    if otp_count == 0:
                        cursor.execute('''INSERT INTO otp_requests (contact, otp_count, otp_sent_at)
                                          VALUES (%s, %s, NOW())''', (contact, 1))
                    else:
                        # Increment the OTP count for this contact
                        cursor.execute('''UPDATE otp_requests SET otp_count = otp_count + 1, otp_sent_at = NOW()
                                          WHERE contact = %s''', (contact,))

                    db.commit()  # Commit the changes to the database
                except Exception as e:
                    logging.error(f"Failed to send OTP email: {e}")
                    flash('Failed to send OTP. Please try again.')
                    return render_template('my_orders.html', otp_required=False, submitted=True)
            else:
                # Handle phone number logic if needed (e.g., SMS OTP via an API)
                flash('OTP sent to your phone number.')
                if otp_count == 0:
                    cursor.execute('''INSERT INTO otp_requests (contact, otp_count, otp_sent_at)
                                      VALUES (%s, %s, NOW())''', (contact, 1))
                else:
                    cursor.execute('''UPDATE otp_requests SET otp_count = otp_count + 1, otp_sent_at = NOW()
                                      WHERE contact = %s''', (contact,))
                db.commit()  # Commit the changes to the database

            return render_template('my_orders.html', otp_required=True, contact=contact, submitted=True)

    return render_template('my_orders.html', otp_required=False, submitted=False)





@bp.route('/favorites', methods=['GET', 'POST'])
def favorites():
    if request.method == 'POST':
       favorites = request.form.getlist('favorites')
    else:
       favorites = request.args.getlist('favorites')
    print(f"Incoming data {request.url}")
    print(f'Favorites parameter {favorites}')
    products = []
    if favorites:
       favorites_list = tuple(favorites)
       print(f" Favorites List: {favorites_list}")
       query = """ select * from products where product_id IN %s AND product_quantity > 0"""
       try:
          db = get_db()
          cursor = db.cursor()
          cursor.execute(query, (favorites_list,))
          products = cursor.fetchall()
          print(f"Fetched from query products {products}")
          cursor.close()

       except Exception as e:
          print(f"Error fetching favorites: {e}")
    return render_template('favorites.html', products=products)



