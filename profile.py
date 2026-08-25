from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from .db import get_db
from .auth import login_required
from .rx_powers import normalize_rows

bp = Blueprint('profile', __name__, url_prefix='/profile')


@bp.route('/')
@login_required
def profile_page():
    user_id = session.get('user_id')
    user_email = session.get('user_email')
    db = get_db()
    cursor = db.cursor()

    # Get customer info (by user_id first, fallback to email)
    customer = None
    if user_id:
        cursor.execute("SELECT customer_id, customer_name, customer_email, customer_phone, date_created FROM customers WHERE customer_id = %s LIMIT 1", (user_id,))
        customer = cursor.fetchone()
    if not customer and user_email:
        cursor.execute("SELECT customer_id, customer_name, customer_email, customer_phone, date_created FROM customers WHERE customer_email = %s ORDER BY customer_id LIMIT 1", (user_email,))
        customer = cursor.fetchone()

    # Get all addresses for this customer (by user_id + email fallback)
    addresses = []
    if user_id:
        cursor.execute("SELECT * FROM customers_address WHERE customer_id = %s ORDER BY address_id DESC", (user_id,))
        addresses = cursor.fetchall()
    if not addresses and user_email:
        cursor.execute(
            "SELECT ca.* FROM customers_address ca JOIN customers c ON ca.customer_id = c.customer_id "
            "WHERE c.customer_email = %s ORDER BY ca.address_id DESC",
            (user_email,)
        )
        addresses = cursor.fetchall()

    # Get recent orders (with payment, prescription, product details)
    orders = []
    order_query = (
        "SELECT o.order_id, o.order_quantity, o.order_total, os.order_status_name, o.date_created, "
        "p.product_name, p.product_image, p.product_special_price, p.product_code, p.product_category, "
        "rc.right_eye, rc.left_eye, rc.recommendations, "
        "rc.addon_1_name, rc.addon_1_price, rc.addon_2_name, rc.addon_2_price, "
        "rc.addon_3_name, rc.addon_3_price, "
        "pc.status as payment_status, pc.date_created as payment_date "
        "FROM orders o "
        "JOIN order_status os ON o.order_id = os.order_id "
        "JOIN products p ON o.product_id = p.product_id "
        "LEFT JOIN rx_collector rc ON rc.rx_id = o.rx_id "
        "LEFT JOIN payment_collector pc ON pc.order_id = o.order_id "
    )
    if user_id:
        cursor.execute(
            order_query + "WHERE o.customer_id = %s ORDER BY o.date_created DESC LIMIT 30",
            (user_id,)
        )
        orders = cursor.fetchall()
    if not orders and user_email:
        cursor.execute(
            order_query +
            "JOIN customers c ON o.customer_id = c.customer_id "
            "WHERE c.customer_email = %s ORDER BY o.date_created DESC LIMIT 30",
            (user_email,)
        )
        orders = cursor.fetchall()

    normalize_rows(orders)

    # Group orders by order_id for display
    from collections import OrderedDict
    grouped_orders = OrderedDict()
    for o in orders:
        oid = o['order_id']
        if oid not in grouped_orders:
            grouped_orders[oid] = {
                'order_id': oid,
                'date_created': o['date_created'],
                'order_status_name': o['order_status_name'],
                'payment_status': o.get('payment_status'),
                'payment_date': o.get('payment_date'),
                'items': [],
                'grand_total': 0,
            }
        grouped_orders[oid]['items'].append(o)
        grouped_orders[oid]['grand_total'] += (o['order_total'] or 0)
    grouped_orders = list(grouped_orders.values())

    # Get face measurement data
    face_data = None
    cust_id = customer['customer_id'] if customer else user_id
    if cust_id:
        cursor.execute(
            "SELECT pd_far, pd_near, face_width, eye_mouth, "
            "recommended_diameter, recommended_bridge, recommended_length, "
            "decentration, frame_candidates, "
            "screenshot_path, measured_at "
            "FROM face_measurements WHERE customer_id = %s "
            "ORDER BY measured_at DESC LIMIT 1",
            (cust_id,)
        )
        face_data = cursor.fetchone()
        # Parse frame_candidates JSON if present
        if face_data and face_data.get('frame_candidates'):
            import json as _json
            try:
                face_data = dict(face_data)
                face_data['frame_candidates_list'] = _json.loads(face_data['frame_candidates'])
            except Exception:
                face_data['frame_candidates_list'] = []

    # Always use session email (authenticated email) for display, not DB record
    auth_email = session.get('user_email', '')
    return render_template('profile.html', customer=customer, addresses=addresses, orders=grouped_orders, auth_email=auth_email, face_data=face_data)


@bp.route('/update', methods=['POST'])
@login_required
def update_profile():
    user_id = session.get('user_id')
    name = request.form.get('name', '').strip()
    phone = request.form.get('phone', '').strip()

    if not name:
        flash('Name is required.', 'danger')
        return redirect(url_for('profile.profile_page'))

    db = get_db()
    cursor = db.cursor()
    try:
        cursor.execute(
            "UPDATE customers SET customer_name = %s, customer_phone = %s WHERE customer_id = %s",
            (name, phone, user_id)
        )
        db.commit()
        session['user_name'] = name
        flash('Profile updated.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error updating profile: {e}', 'danger')

    return redirect(url_for('profile.profile_page'))


@bp.route('/address/add', methods=['POST'])
@login_required
def add_address():
    user_id = session.get('user_id')
    address = request.form.get('address', '').strip()
    state = request.form.get('state', '').strip()
    zipcode = request.form.get('zipcode', '').strip()
    country = request.form.get('country', '').strip() or 'India'
    phone = request.form.get('phone', '').strip()

    if not address:
        flash('Address is required.', 'danger')
        return redirect(url_for('profile.profile_page'))

    db = get_db()
    cursor = db.cursor()
    try:
        cursor.execute(
            "INSERT INTO customers_address (customer_id, address, state, zipcode, country) VALUES (%s, %s, %s, %s, %s)",
            (user_id, address, state, zipcode, country)
        )
        # Also update phone if provided
        if phone:
            cursor.execute("UPDATE customers SET customer_phone = %s WHERE customer_id = %s", (phone, user_id))
        db.commit()
        flash('Address added.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error adding address: {e}', 'danger')

    return redirect(url_for('profile.profile_page'))


@bp.route('/address/edit/<int:address_id>', methods=['POST'])
@login_required
def edit_address(address_id):
    user_id = session.get('user_id')
    address = request.form.get('address', '').strip()
    state = request.form.get('state', '').strip()
    zipcode = request.form.get('zipcode', '').strip()
    country = request.form.get('country', '').strip() or 'India'

    if not address:
        flash('Address is required.', 'danger')
        return redirect(url_for('profile.profile_page'))

    db = get_db()
    cursor = db.cursor()
    try:
        # Verify address belongs to this user (or same email)
        cursor.execute(
            "SELECT ca.address_id FROM customers_address ca "
            "JOIN customers c ON ca.customer_id = c.customer_id "
            "WHERE ca.address_id = %s AND c.customer_email = %s",
            (address_id, session.get('user_email'))
        )
        if not cursor.fetchone():
            flash('Address not found.', 'danger')
            return redirect(url_for('profile.profile_page'))

        cursor.execute(
            "UPDATE customers_address SET address = %s, state = %s, zipcode = %s, country = %s WHERE address_id = %s",
            (address, state, zipcode, country, address_id)
        )
        db.commit()
        flash('Address updated.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error updating address: {e}', 'danger')

    return redirect(url_for('profile.profile_page'))


@bp.route('/address/delete/<int:address_id>', methods=['POST'])
@login_required
def delete_address(address_id):
    db = get_db()
    cursor = db.cursor()
    try:
        # Verify address belongs to this user
        cursor.execute(
            "SELECT ca.address_id FROM customers_address ca "
            "JOIN customers c ON ca.customer_id = c.customer_id "
            "WHERE ca.address_id = %s AND c.customer_email = %s",
            (address_id, session.get('user_email'))
        )
        if not cursor.fetchone():
            flash('Address not found.', 'danger')
            return redirect(url_for('profile.profile_page'))

        cursor.execute("DELETE FROM customers_address WHERE address_id = %s", (address_id,))
        db.commit()
        flash('Address deleted.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error deleting address: {e}', 'danger')

    return redirect(url_for('profile.profile_page'))


@bp.route('/address/set-default/<int:address_id>', methods=['POST'])
@login_required
def set_default_address(address_id):
    """Set an address as the default (most recent) by updating its address_id to be highest."""
    # We just store the preference in session for checkout
    session['default_address_id'] = address_id
    flash('Default address set.', 'success')
    return redirect(url_for('profile.profile_page'))
