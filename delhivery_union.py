import requests
import os
import json
import logging
import subprocess
import pymysql
from datetime import datetime

# Configure logging for debugging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# API credentials and constants
API_TOKEN = os.environ.get("DELHIVERY_API_TOKEN", "")
BASE_URL = "https://track.delhivery.com/api"
PICKUP_DETAILS = {
    "name": "SF KRISHNA 0108807",
    "pin": "122004",
    "add": "Unit 418, Tower 4, 4th Floor, Gurgaon Sector 74A, Gurgaon, Haryana, India 122004",
    "phone": "9810113801",
    "state": "Haryana",
    "city": "Gurgaon",
    "country": "India"
}

def get_customer_data(order_id):
    """Fetch customer data from the database for the given order ID."""
    connection = pymysql.connect(
        host='172.105.54.11',
        user='oslb6',
        password=os.environ.get('MYSQL_PASSWORD', ''),
        database='optiwar2'
    )
    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            query = (
                "SELECT o.order_id, c.customer_name AS cust_name, c.customer_phone AS cust_phone, "
                "o.date_created AS date_created, o.order_total AS total, ca.address AS shipping_address, "
                "ca.state AS shipping_zone, ca.state AS shipping_city, ca.zipcode AS shipping_postcode "
                "FROM orders AS o "
                "JOIN customers AS c ON c.customer_id = o.customer_id "
                "LEFT JOIN customers_address AS ca ON ca.customer_id = o.customer_id "
                "WHERE o.order_id = %s"
            )
            cursor.execute(query, (order_id,))
            result = cursor.fetchone()
            if result:
                result["payment_mode"] = "Prepaid"
                result["cod_amount"] = 0
                logging.debug(f"Customer data fetched: {result}")
                return result
            else:
                logging.error("No customer data found for the given order ID.")
                return None
    except Exception as e:
        logging.error(f"Database query failed: {e}")
        return None
    finally:
        connection.close()

def generate_waybill(order_id):
    """Generate a waybill using the Delhivery API."""
    customer = get_customer_data(order_id)
    if not customer:
        logging.error("Unable to generate waybill due to missing customer data.")
        return None

    # Serialize datetime object to a string
    if isinstance(customer["date_created"], datetime):
        customer["date_created"] = customer["date_created"].strftime("%Y-%m-%d %H:%M:%S")

    # Prepare the request payload with format=json and the data key
    payload = {
        "pickup_location": PICKUP_DETAILS,
        "shipments": [
            {
                "return_name": PICKUP_DETAILS["name"],
                "return_pin": PICKUP_DETAILS["pin"],
                "return_city": PICKUP_DETAILS["city"],
                "return_phone": PICKUP_DETAILS["phone"],
                "return_add": PICKUP_DETAILS["add"],
                "return_state": PICKUP_DETAILS["state"],
                "return_country": PICKUP_DETAILS["country"],
                "shipping_mode": "Surface",
                "order": order_id,
                "phone": customer["cust_phone"],
                "products_desc": "contact lens",
                "cod_amount": customer["cod_amount"],
                "name": customer["cust_name"],
                "country": "India",
                "order_date": customer["date_created"],
                "total_amount": customer["total"],
                "add": customer["shipping_address"],
                "pin": customer["shipping_postcode"],
                "payment_mode": customer["payment_mode"],
                "state": customer["shipping_zone"],
                "city": customer["shipping_city"],
                "client": PICKUP_DETAILS["name"],
            }
        ]
    }

    # Convert payload to JSON and prepend format=json
    body = f"format=json&data={json.dumps(payload)}"

    url = f"{BASE_URL}/cmu/create.json"
    headers = {
        "Authorization": f"Token {API_TOKEN}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        response = requests.post(url, headers=headers, data=body)
        response.raise_for_status()
        data = response.json()

        logging.debug(f"Raw API Response: {response.text}")
        logging.debug(f"Parsed API Response: {json.dumps(data, indent=4)}")

        if data.get("packages"):
            package = data["packages"][0]
            waybill = package.get("waybill")
            remarks = package.get("remarks", [None])[0]

            if waybill:
                logging.info(f"Waybill generated successfully: {waybill}")
                return waybill
            elif remarks:
                logging.warning(f"API Remarks: {remarks}")
            else:
                logging.error("Failed to generate waybill. No waybill or remarks in the response.")
        else:
            logging.error("Failed to generate waybill. No 'packages' key in the response.")
    except requests.exceptions.RequestException as e:
        logging.error(f"HTTP request failed: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
    return None

def fetch_and_print_pdf(waybill, output_file, printer_name="default"):
    """Fetch the PDF for the given waybill and print it."""
    url = f"{BASE_URL}/p/packing_slip?wbns={waybill}&pdf=true"
    headers = {
        "Authorization": f"Token {API_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        pdf_url = data.get("packages", [{}])[0].get("pdf_download_link")
        if not pdf_url:
            logging.error("No PDF download link found.")
            return False

        # Download the PDF
        pdf_response = requests.get(pdf_url)
        pdf_response.raise_for_status()

        # Save the PDF locally
        with open(output_file, "wb") as file:
            file.write(pdf_response.content)

        # Print the PDF using lp
        print_command = ["lp", "-d", printer_name, output_file]
        subprocess.run(print_command, check=True)
        logging.info(f"Printed PDF for waybill {waybill} successfully.")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Printing failed: {e}")
    except Exception as e:
        logging.error(f"Error fetching or saving PDF: {e}")
    return False

# Main script
if __name__ == "__main__":
    order_id = "LVVP-5850"  # Replace with a real order ID
    waybill = generate_waybill(order_id)
    if waybill:
        pdf_path = f"/tmp/{waybill}.pdf"
        if not fetch_and_print_pdf(waybill, pdf_path):
            logging.info(f"PDF saved locally at {pdf_path} for manual handling.")


