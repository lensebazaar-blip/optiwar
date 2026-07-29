from flask import Flask, Blueprint, request, render_template, session, redirect, url_for, flash

bp = Blueprint('cl_range', __name__)


def format_range(values, include_plano=False):
    result = [f"{v/100.0:.2f}" for v in values]
    if include_plano:
        result = ["PLANO" if val == "0.00" else val for val in result]
    return result

def cyl_range():
    cyl = [round(i, 2) for i in frange(0.00, -6.25, -0.25)]
    cyl += [round(i, 2) for i in frange(-6.50, -8.50, -0.50)]
    return [f"{v:.2f}" for v in cyl]

def axis_range():
    return [str(i) for i in range(0,181,5)]

def add_range():
    add = [round(i, 2) for i in frange(1.00, 3.25, 0.25)]
    return [f"{v:.2f}" for v in add]

def frange(start, stop, step):
    while (step > 0 and start < stop) or (step < 0 and start > stop):
       yield start
       start += step
 
def get_power_range(product_id):
    if product_id == '1005':
          first = [round(i, 2) for i in frange(0.00, -6.25, -0.25)]
          second = [round(i, 2) for i in frange(-6.50, -9.50, -0.50)]
          #second = [round(i,2) for i in frange(-6.50, -20.5, -0.50)]
          #third = [round(i, 2) for i in frange(0.25, 6.25, 0.25)]
          full_range = first + second
          return [f"{v:.2f}" for v in full_range]
    return []

def get_color_range(product_id):
    color_map = {
    '1005': [{'name': 'Azure Blue', 'img': 'azure_blue.jpeg'},
             {'name': 'Glitter Grey', 'img': 'glitter_gray.jpeg'}, 
             {'name': 'Sandy Brown','img': 'glitter_gray.jpeg'},
             {'name' :'Tan Brown','img':'glitter_gray.jpeg'},
             {'name': 'Black', 'img': 'glitter_gray.jpeg'},
             {'name':'Dark Green','img':'glitter_gray.jpeg'},
             {'name':'Salted Blue','img':'glitter_gray.jpeg'},
             {'name':'Natural Brown','img':'glitter_gray.jpeg'},
             {'name': 'Charcoal Gray','img':'glitter_gray.jpeg'},
             {'name': 'Earth Gray', 'img':'glitter_gray.jpeg'},
             {'name':'Pure Hazel', 'img':'glitter_gray.jpeg'},
             {'name':'Vivid Blue','img':'glitter_gray.jpeg'},
             {'name': 'Caribbean Green', 'img':'glitter_gray.jpeg'}],
    }
    return color_map.get(product_id, [])



@bp.route('/add_prescription_of_cl', methods=['GET', 'POST'])
def add_prescription_of_cl():
    product_id = request.form.get('product_id')
    product_name = request.form.get('product_name')
    product_code = request.form.get('product_code')
    product_special_price = int(float(request.form.get('product_special_price', 0)))
    product_price = int(float(request.form.get('product_price', 0)))
    product_category = request.form.get('product_category', 'category_not_defined')
    order_quantity = int(request.form.get('order_quantity', 1))
    print(f"This is CL Range Route ")
    pwr_range = get_power_range(product_id)
    cyls = cyl_range()
    axises = axis_range()
    adds = add_range()
    color_range = get_color_range(product_id)

    return render_template('lens.html',
                           product_id=product_id,
                           product_name=product_name,
                           product_code=product_code,
                           product_special_price=product_special_price,
                           product_price=product_price,
                           product_category=product_category,
                           order_quantity=order_quantity,
                           pwr_range=pwr_range,
                           cyl_range=cyls,
                           axis_range=axises,
                           add_range=adds,
                           color_range=color_range
                          )



'''
@bp.route('/add_prescription_of_cl', methods=['GET', 'POST'])
def add_prescription_of_cl():
    product_id = request.form.get('product_id')
    product_name = request.form.get('product_name')
    product_special_price = int(float(request.form.get('product_special_price', 0)))
    product_price = int(float(request.form.get('product_price', 0)))
    product_category = request.form.get('product_category', 'category_not_defined')
    order_quantity = int(request.form.get('order_quantity', 1))
    print(f"This is CL Range Route ")
    pwr_range = get_power_range(product_id)
    cyls = cyl_range()
    adds = add_range()
    colors = get_color_range(product_id)

    cart = session.get('cart', [])
    print(f"This is CL Range Model {cart}")
    for item in cart:
        if any([
            item.get('right_pwr'), item.get('left_pwr'),
            item.get('right_cyl'), item.get('left_cyl')
        ]):
            flash("Prescription already exist for this product. Choose another product or colear your cart.")
            return redirect(url_for('main.checkout'))
        else:
            cart = [i for i in cart if i['product_id'] != product_id]
            session['cart'] = cart
            break
    return render_template('lens.html',
                           product_id=product_id,
                           product_name=product_name,
                           product_special_price=product_special_price,
                           product_price=product_price,
                           product_category=product_category,
                           order_quantity=order_quantity,
                           pwr_range=pwr_range,
                           cyl_range=cyls,
                           axis_range=axises,
                           add_range=adds,
                           color_range=colors
                          )

'''
