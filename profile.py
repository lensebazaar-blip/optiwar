from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify
from .db import get_db
from .auth import login_required
from .rx_powers import normalize_rows
from .customer_orders import ORDER_LINES_SQL, customer_orders, attach_reship
from . import reship
from . import face_profiles, face_profiles_api, face_scan_groups, face_scan_invites_api

bp = Blueprint('profile', __name__, url_prefix='/profile')

# ?tab= values the page renders active on first paint; "faces" is the Faces
# destination, a bare /profile/ is Account.
TABS = {'account': 'account', 'addresses': 'addresses', 'orders': 'orders',
        'faces': 'myface', 'myface': 'myface'}


def active_tab(args):
    return TABS.get((args.get('tab') or '').strip().lower(), 'account')


def focus_face(args):
    try:
        return int(args.get('face') or 0) or None
    except (TypeError, ValueError):
        return None


@bp.route('/faces')
@login_required
def faces_page():
    """The header's Faces destination: manage people and measurements, not
    the scanner."""
    return redirect(url_for('profile.profile_page', tab='faces'))


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

    # Order lines (latest status + payment proof per order); only paid orders
    # survive customer_orders(). LIMIT counts lines, not orders, so it is wide
    # enough that unpaid attempts do not push real orders off the page.
    orders = []
    if user_id:
        cursor.execute(
            ORDER_LINES_SQL + "WHERE o.customer_id = %s ORDER BY o.date_created DESC LIMIT 300",
            (user_id,)
        )
        orders = cursor.fetchall()
    if not orders and user_email:
        cursor.execute(
            ORDER_LINES_SQL +
            "JOIN customers c ON o.customer_id = c.customer_id "
            "WHERE c.customer_email = %s ORDER BY o.date_created DESC LIMIT 300",
            (user_email,)
        )
        orders = cursor.fetchall()

    normalize_rows(orders)
    grouped_orders = customer_orders(orders)[:30]
    cust_id = customer['customer_id'] if customer else user_id
    reship_rows = {}
    if grouped_orders and cust_id and reship.enabled() and reship.is_india_host(request.host):
        try:
            reship.ensure_schema(db)
            reship_rows = reship.for_customer(db, cust_id)
        except Exception:  # noqa: BLE001 - the order list must still render
            reship_rows = {}
    attach_reship(grouped_orders, reship_rows, request.host)

    # Get face measurement data
    face_data = None
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

    # Multi-person accounts (Stage-1 gate): the tab becomes My Faces and lists
    # every profile; everybody else keeps the single-face card above.
    face_profiles_enabled = False
    face_remote_scan_enabled = False
    face_profile_list = []
    face_scan_groups_open = []
    if cust_id and face_profiles_api.gate_enabled():
        face_profiles_enabled = True
        face_profiles.ensure_schema(db)
        face_profiles.migrate_customer(
            db, cust_id, customer['customer_name'] if customer else None)
        face_profile_list = [
            face_profiles.public_view(
                r, "/api/face-profiles/%d/capture" % int(r['id']))
            for r in face_profiles.list_profiles(db, cust_id)]
        face_remote_scan_enabled = face_scan_invites_api.gate_enabled()
        if face_remote_scan_enabled:
            face_scan_invites_api.attach_state(db, cust_id, face_profile_list)
            face_scan_groups.ensure_schema(db)
            face_scan_groups_open = face_scan_groups.for_customer(db, cust_id)

    # Always use session email (authenticated email) for display, not DB record
    auth_email = session.get('user_email', '')
    return render_template('profile.html', customer=customer, addresses=addresses,
                           orders=grouped_orders, auth_email=auth_email,
                           face_data=face_data,
                           face_profiles_enabled=face_profiles_enabled,
                           face_profiles=face_profile_list,
                           face_remote_scan_enabled=face_remote_scan_enabled,
                           face_scan_groups_open=face_scan_groups_open,
                           active_tab=active_tab(request.args),
                           focus_face=focus_face(request.args),
                           face_relationships=[
                               {'code': c, 'label': face_profiles.RELATIONSHIP_LABELS[c]}
                               for c in face_profiles.RELATIONSHIPS
                               if c != face_profiles.REL_SELF])


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
