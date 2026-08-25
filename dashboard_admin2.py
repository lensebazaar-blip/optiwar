import os
import logging
import pymysql
from prettytable import PrettyTable
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


logging.basicConfig(
    filename="dashboard_backend.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)


EMAIL_CONFIG = {
    'sender_email': 'admin@optiwar.com',
    'email_password': os.environ.get('MAIL_PASSWORD', ''),
    'smtp_server': '192.168.0.78',
    'smtp_port': 587
}


DB_CONFIG = {
    "host": "172.105.54.11",
    "user": "oslb6",
    "password": os.environ.get('MYSQL_PASSWORD', ''),
    "database": "optiwar2"
}


def fetch_data_from_db():
    """Fetch data from the database based on the provided SQL query."""
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    query = """
    SELECT o.date_created, os.order_status_name, ca.country, o.order_id, p.box_number,
           p.product_code AS PRODUCT_CODE, p.product_name, c.customer_id, c.customer_phone, c.customer_email,
           ca.address, ca.state, ca.zipcode, o.order_total, pc.status AS Payment_status
    FROM orders o
    LEFT JOIN rx_collector rc ON rc.rx_id = o.rx_id
    LEFT JOIN customers_address ca ON ca.customer_id = o.customer_id
    LEFT JOIN order_status os ON os.order_id = o.order_id
    LEFT JOIN payment_collector pc ON pc.order_id = o.order_id
    LEFT JOIN customers c ON c.customer_id = o.customer_id
    JOIN products p ON p.product_id = o.product_id
    GROUP BY o.order_id, ca.country, rc.recommendations, rc.right_eye, rc.left_eye, o.order_total, p.product_code
    ORDER BY o.date_created;


    """
    #GROUP BY o.order_id, ca.country, rc.recommendations, rc.right_eye, rc.left_eye, o.order_total


    cursor.execute(query)
    data = cursor.fetchall()
    conn.close()
    return data

def get_terminal_size():
    """Get the terminal width for dynamic column adjustment."""
    size = os.get_terminal_size()
    return size.columns

def truncate_or_wrap(value, max_width):
    """Truncate or wrap the value based on the maximum width."""
    value_str = str(value)
    if len(value_str) > max_width:
        return value_str[:max_width - 3] + "..."
    return value_str

def display_orders():
    """Fetch and display order data dynamically adjusted for screen size."""
    data = fetch_data_from_db()

    if not data:
        print("No Data Available")
        return

    terminal_width = get_terminal_size()
    max_column_width = max(10, terminal_width // len(data[0].keys()) - 2)  # Adjust column width dynamically

    table = PrettyTable()
    table.field_names = data[0].keys()

    for row in data:
        adjusted_row = {key: truncate_or_wrap(value, max_column_width) if isinstance(value, str) else value for key, value in row.items()}
        table.add_row(adjusted_row.values())

    print(table)

def update_customer_address():
    """Update a customer address."""
    conn = pymysql.connect(**DB_CONFIG)
    cursor = conn.cursor()

    customer_id = input("Enter the customer ID to update address: ")
    new_address = input("Enter the new address: ")

    update_query = """
    UPDATE customers_address
    SET address = %s
    WHERE customer_id = %s;
    """
    cursor.execute(update_query, (new_address, customer_id))
    conn.commit()
    print("Customer address updated successfully!")
    conn.close()



def update_customer_email():
    """Update customer email based on order ID using JOIN."""
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()

        logging.info("Starting the update_customer_email process.")

        order_id = input("Enter the Order ID (e.g., AABB-1212): ")
        logging.info("User provided Order ID: %s", order_id)

        # Retrieve customer_id and customer_email using order_id
        fetch_customer_query = """
        SELECT o.customer_id, c.customer_email
        FROM orders AS o
        JOIN customers AS c ON c.customer_id = o.customer_id
        WHERE o.order_id = %s;
        """
        cursor.execute(fetch_customer_query, (order_id,))
        result = cursor.fetchone()

        if not result:
            print("No customer found for the given Order ID.")
            logging.warning("No customer found for order_id %s.", order_id)
            return

        customer_id, old_email = result
        logging.info(
            "Retrieved Customer ID: %s and Old Email: %s for Order ID: %s",
            customer_id, old_email, order_id
        )

        print(f"Old Customer Email: {old_email}, Customer ID: {customer_id}, associated with Order ID: {order_id}")

        new_email = input("Enter the new email address: ")
        logging.info("User provided new email: %s", new_email)

        update_query = """
        UPDATE customers
        SET customer_email = %s
        WHERE customer_id = %s;
        """

        cursor.execute(update_query, (new_email, customer_id))
        conn.commit()

        if cursor.rowcount > 0:
            print("Customer email details updated successfully!")
            logging.info(
                "Successfully updated email for customer_id %s from %s to %s",
                customer_id, old_email, new_email
            )
        else:
            print("No customer found with the given ID.")
            logging.warning("No update made: customer_id %s not found.", customer_id)

    except KeyboardInterrupt:
        print("\nI have not updated anything, but I have exited.")
        logging.warning("Operation interrupted by user via KeyboardInterrupt.")
    except Exception as e:
        print(f"An error occurred: {e}")
        logging.error("Error during update: %s", e, exc_info=True)
    finally:
        if conn and conn.open:
            conn.close()
            logging.info("Database connection closed.")
            print("Connection closed.")


def send_email_to_customer_with_order_id():
    """Send an order shipped email to the customer based on the Order ID."""
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()

        logging.info("Starting the Email Sending process.")

        order_id = input("Enter the Order ID (e.g., AABB-1212): ")
        awb_number = input("Enter AWB Delhivery: ")
        ship_date = input("Input shipping date (DD/MM/YYYY): ")
        logging.info("User provided Order ID: %s", order_id)

        # Retrieve customer details using order_id
        fetch_customer_query = """
        SELECT c.customer_email, c.customer_name, ca.address
        FROM orders o
        LEFT JOIN customers c ON c.customer_id = o.customer_id
        LEFT JOIN customers_address ca ON ca.customer_id = o.customer_id
        WHERE o.order_id = %s;
        """
        cursor.execute(fetch_customer_query, (order_id,))
        result = cursor.fetchone()

        if not result:
            print("No customer found for the given Order ID.")
            logging.warning("No customer found for order_id %s.", order_id)
            return

        customer_email, customer_name, customer_address = result

        # Prepare the email content
        html_body = f"""
        <body style="background-color:#f0f0f0; font-family: Arial, sans-serif;">
            <table align="center" border="0" cellpadding="0" cellspacing="0" width="600" bgcolor="white" style="border:2px solid #cccccc; margin: 20px auto;">
                <tbody>
                    <tr>
                        <td align="center" style="background-color: #4cb96b; padding: 20px;">
                            <p style="color:white; font-size: 24px; font-weight:bold;">
                                Order Shipped - Order ID: {order_id}
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 20px;">
                            <p style="font-size: 18px; color:#333333;">
                                Hello {customer_name},<br> Thanking you again for your trust and order!<br><br>
                                We have shipped your order via Delhivery AWB {awb_number} on {ship_date}.<br><br>
                                Please co-operate with delivery Agents to receive your order timely and avoid returns<br><br>
                                Your order is expected to reach you soon.
                            </p>
                        </td>
                    </tr>
                    <tr>
                        <td align="center" style="padding: 20px;">
                            <p style="font-size: 16px; color:#333333;">
                                Do us a favour and mark this email as safe, as we will not spam your mailbox :) <br>
                                If you have any questions, simply reply to this email.<br>
                                <a href="https://optiwar.com" style="color: #4cb96b; text-decoration: none; font-weight: bold;">Visit Optiwar</a> <br> Factory Outlet Opticals
                            </p>
                        </td>
                    </tr>
                </tbody>
            </table>
        </body>
        """

        # Send email
        send_email(customer_email, html_body, order_id, awb_number)
        print(f"Email sent successfully to {customer_email}.")
        logging.info("Email sent successfully to %s.", customer_email)

    except KeyboardInterrupt:
        print("\nOperation interrupted by user.")
        logging.warning("Operation interrupted by user via KeyboardInterrupt.")
    except Exception as e:
        print(f"An error occurred: {e}")
        logging.error("Error during email sending: %s", e, exc_info=True)
    finally:
        if conn and conn.open:
            conn.close()
            logging.info("Database connection closed.")
            print("Connection closed.")

def send_email(to_email, html_content, order_id, awb_number):
    """Function to send an HTML email."""
    try:
        msg = MIMEMultipart()
        msg['From'] = EMAIL_CONFIG['sender_email']
        msg['To'] = to_email
        msg['Subject'] = f"Your Optiwar order {order_id} has been Shipped! via Delhivery AWB {awb_number}"
        msg.attach(MIMEText(html_content, 'html'))

        server = smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'])
        server.starttls()
        server.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['email_password'])
        server.sendmail(EMAIL_CONFIG['sender_email'], to_email, msg.as_string())
        server.quit()

        logging.info("Email successfully sent to %s", to_email)
    except Exception as e:
        logging.error("Failed to send email to %s: %s", to_email, e)
        print(f"Failed to send email: {e}")


def update_order_status_table_for_order_id():
    """Update order status from Processed to Shipped."""
    conn = None
    try:
        conn = pymysql.connect(**DB_CONFIG)
        cursor = conn.cursor()

        order_id = input("Enter the order id: ")

        # Append the new status: the previous UPDATE rewrote every status row
        # this order had, so a later Complete became Shipped again.
        cursor.execute(
            "INSERT INTO order_status (order_status_name, order_id) VALUES ('Shipped', %s)",
            (order_id,))
        conn.commit()
        print("Order Status Shipped recorded successfully!")

        cursor.execute(
            "SELECT os.order_status_id, os.order_status_name FROM order_status os "
            "WHERE os.order_id = %s ORDER BY os.order_status_id;", (order_id,))
        print(cursor.fetchall())
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        if conn and conn.open:
            conn.close()

def main():
    while True:
        print("\nOrder Processing Dashboard")
        print("1. View Processed Orders")
        print("2. Make Changes to Customer Address")
        print("3. Make Changes to Customers (Update by Email)")
        print("4. Send Shipped Mails to Customer using Order ID")
        print("5. Update Order Status from Processed to Shipped")
        print("6. Exit Dashboard")

        choice = input("Select an option: ")

        if choice == '1':
            display_orders()
        elif choice == '2':
            update_customer_address()
        elif choice == '3':
            update_customer_email()
        elif choice == '4':
            send_email_to_customer_with_order_id()
        elif choice == '5':
            update_order_status_table_for_order_id()
        elif choice == '6':
            print("Exiting Dashboard.")
            break
        else:
            print("Invalid option, please try again.")

if __name__ == "__main__":
    main()

