import os
import streamlit as st
import pymysql
import pandas as pd
import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_db_connection():
    """Create a secure database connection."""
    try:
        conn = pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "localhost"),
            user=os.environ.get("MYSQL_ADMIN_USER", "admin"),
            password=os.environ.get("MYSQL_ADMIN_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "optiwar2"),
            cursorclass=pymysql.cursors.DictCursor
        )
        logger.info("Database connection established.")
        return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise

def fetch_data():
    """Fetch data from the database."""
    query = """
    SELECT o.date_created, os.order_status_name, ca.country, o.order_id,
           p.product_code AS PRODUCT_CODE, c.customer_phone, c.customer_email,
           ca.address, ca.state, ca.zipcode, o.order_total, pc.status AS Payment_status
    FROM orders o
    LEFT JOIN rx_collector rc ON rc.rx_id = o.rx_id
    LEFT JOIN customers_address ca ON ca.customer_id = o.customer_id
    LEFT JOIN order_status os ON os.order_id = o.order_id
    LEFT JOIN payment_collector pc ON pc.order_id = o.order_id
    LEFT JOIN customers c ON c.customer_id = o.customer_id
    LEFT JOIN products p ON p.product_id = o.product_id
    GROUP BY o.order_id, ca.country, rc.recommendations, rc.right_eye, rc.left_eye, o.order_total
    ORDER BY o.date_created;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                logger.debug("Fetching data from database.")
                cursor.execute(query)
                data = cursor.fetchall()
        logger.info("Data fetched successfully.")
        return data
    except Exception as e:
        logger.error(f"Error fetching data: {e}")
        return []

def search_customers_by_email_or_phone(search_term):
    """Search customers by email or phone."""
    query = """
    SELECT customer_id, customer_phone, customer_email
    FROM customers
    WHERE customer_email LIKE %s OR customer_phone LIKE %s;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                logger.debug(f"Executing search query with term: {search_term}")
                cursor.execute(query, (f"%{search_term}%", f"%{search_term}%"))
                results = cursor.fetchall()
        logger.info(f"Search completed for term: {search_term}. Results: {len(results)}")
        return results
    except Exception as e:
        logger.error(f"Error during search: {e}")
        return []

def update_customer_address(customer_id, new_address):
    """Update a customer's address."""
    query = """
    UPDATE customers_address
    SET address = %s
    WHERE customer_id = %s;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                logger.debug(f"Updating address for customer_id={customer_id}, new_address={new_address}")
                cursor.execute(query, (new_address, customer_id))
                conn.commit()
                if cursor.rowcount > 0:
                    logger.info(f"Address updated for customer ID: {customer_id}.")
                    return "Customer address updated successfully!"
                else:
                    logger.warning(f"No rows updated for customer ID: {customer_id}.")
                    return "No changes made to the address."
    except Exception as e:
        logger.error(f"Error updating address for customer ID {customer_id}: {e}")
        return "Failed to update customer address."

def main():
    st.title("Order Processing Dashboard")

    menu = ["View Processed Orders", "Update Customer Address"]
    choice = st.sidebar.selectbox("Menu", menu)

    if choice == "View Processed Orders":
        st.subheader("Processed Orders")
        data = fetch_data()
        if data:
            df = pd.DataFrame(data)
            st.dataframe(df)
        else:
            st.warning("No data available.")

    elif choice == "Update Customer Address":
        st.subheader("Update Customer Address")
        search_term = st.text_input("Enter Customer Email or Phone")
        if st.button("Search"):
            if search_term:
                results = search_customers_by_email_or_phone(search_term)
                if results:
                    options = {f"{res['customer_email']} ({res['customer_phone']})": res['customer_id'] for res in results}
                    selected = st.selectbox("Select a Customer", options.keys())
                    new_address = st.text_area("Enter New Address")
                    if st.button("Update Address"):
                        customer_id = options[selected]
                        result = update_customer_address(customer_id, new_address)
                        st.success(result)
                        logger.info(f"Address update result: {result}")
                else:
                    st.error("No matching customers found.")
                    logger.warning("No matching customers for search term.")
            else:
                st.error("Please enter a search term.")

if __name__ == "__main__":
    main()

