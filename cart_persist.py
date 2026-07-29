"""
Persistent cart helpers — save/load/clear cart in MySQL `persistent_cart` table.
Cart is stored as a JSON blob keyed by customer_id.
"""
import json
from flask import session, current_app
from .db import get_db


def save_cart_to_db():
    """Persist current session cart to database for the logged-in user.
    Skips saving if the session cart is empty — this prevents a device with
    an empty session from overwriting a cart that was populated on another
    device.  Explicit clearing is handled by clear_cart_in_db().
    """
    user_id = session.get('user_id')
    if not user_id:
        return
    cart = session.get('cart', [])
    if not cart:
        return
    try:
        db = get_db()
        cursor = db.cursor()
        cart_json = json.dumps(cart, default=str)
        cursor.execute(
            'INSERT INTO persistent_cart (customer_id, cart_json) VALUES (%s, %s) '
            'ON DUPLICATE KEY UPDATE cart_json = %s',
            (user_id, cart_json, cart_json)
        )
        db.commit()
    except Exception as e:
        current_app.logger.error(f'[CART PERSIST] Error saving cart for user {user_id}: {e}')


def load_cart_from_db():
    """Restore cart from database into session for the logged-in user.
    Merges: if the current session already has items, those are kept and
    DB items that aren't duplicates are added.
    """
    user_id = session.get('user_id')
    if not user_id:
        return
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            'SELECT cart_json FROM persistent_cart WHERE customer_id = %s',
            (user_id,)
        )
        row = cursor.fetchone()
        if not row or not row['cart_json']:
            return

        db_cart = json.loads(row['cart_json'])
        if not isinstance(db_cart, list) or len(db_cart) == 0:
            return

        current_cart = session.get('cart', [])

        if not current_cart:
            session['cart'] = db_cart
            session.modified = True
            current_app.logger.info(
                f'[CART PERSIST] Restored {len(db_cart)} item(s) from DB for user {user_id}'
            )
            return

        # Merge: add DB items that aren't already in current session cart
        existing_keys = set()
        for item in current_cart:
            key = (str(item.get('product_id', '')), str(item.get('rx_id', '')))
            existing_keys.add(key)

        added = 0
        for item in db_cart:
            key = (str(item.get('product_id', '')), str(item.get('rx_id', '')))
            if key not in existing_keys:
                current_cart.append(item)
                existing_keys.add(key)
                added += 1

        if added > 0:
            session['cart'] = current_cart
            session.modified = True
            current_app.logger.info(
                f'[CART PERSIST] Merged {added} item(s) from DB for user {user_id}'
            )

    except Exception as e:
        current_app.logger.error(f'[CART PERSIST] Error loading cart for user {user_id}: {e}')


def clear_cart_in_db():
    """Remove persistent cart for the logged-in user (after checkout)."""
    user_id = session.get('user_id')
    if not user_id:
        return
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            'DELETE FROM persistent_cart WHERE customer_id = %s',
            (user_id,)
        )
        db.commit()
        current_app.logger.info(f'[CART PERSIST] Cleared DB cart for user {user_id}')
    except Exception as e:
        current_app.logger.error(f'[CART PERSIST] Error clearing cart for user {user_id}: {e}')
