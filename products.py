import logging
from flask import Flask, request, render_template, flash, Blueprint, session, url_for, redirect, make_response
from .db import get_db
from datetime import datetime
from .db import get_db


bp = Blueprint('products', __name__)

logging.basicConfig(level=logging.DEBUG)


@bp.route('/soflens_59', methods=['GET', 'POST'])
def soflens_59():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('select * from products where product_id=1001')
    product = cursor.fetchall()
    print(f"I found products {product}")
    response = make_response(render_template('soflens_59.html', product=product))
    return response
