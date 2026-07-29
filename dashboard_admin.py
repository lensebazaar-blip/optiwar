import mysql.connector
from tabulate import tabulate
from datetime import datetime

db_config = {
   "host": "localhost",
   "user": "admin",
   "password": "users09@",
   "database": "optiwar2"
}

def connect_to_database(config):
    try:
        return mysql.connector.connect(**config)
    except mysql.connector.Error as err:
        print(f"Error connecting to db: {err}")
        return "Connected to Optiwar2 DB"

def price_cost_function_loader(db, cursor):
    """Load and update product prices based on cost with detailed debugging."""
    total_products = 0
    total_updated = 0
    total_skipped = 0

    try:
        # Log the SQL query to ensure it matches expectations
        query = "SELECT product_id, product_name, product_cost FROM products"
        print(f"Executing Query: {query}")
        cursor.execute(query)
        rows = cursor.fetchall()

        # Log the total rows fetched
        total_products = len(rows)
        print(f"Total Products Fetched: {total_products}")

        processed_product_ids = set()  # Track processed product IDs

        for row in rows:
            product_id, product_name, product_cost = row
            processed_product_ids.add(product_id)  # Track this product as processed

            # Log product details for debugging
            print(f"Processing Product: ID={product_id}, Name={product_name}, Cost={product_cost}")

            if product_cost is None:
                print(f"Skipping Product: {product_name} (Cost is None)")
                total_skipped += 1
                continue

            # Calculate initial product price
            product_price = product_cost * 5

            # Adjust product price based on thresholds
            if product_price % 100 < 50:
                product_price = (product_price // 100) * 100 - 1
            else:
                product_price = (product_price // 100) * 100 + 99

            # Update the product price in the database
            try:
                cursor.execute(
                    "UPDATE products SET product_price = %s WHERE product_id = %s",
                    (product_price, product_id)
                )
                db.commit()
                total_updated += 1  # Increment updated products count
                print(f"Updated Product: ID={product_id}, New Price={product_price}")
            except mysql.connector.Error as update_err:
                print(f"Failed to Update Product: ID={product_id}, Error={update_err}")

        # Check for missing or duplicate products
        unique_product_ids_query = "SELECT COUNT(DISTINCT product_id) FROM products"
        cursor.execute(unique_product_ids_query)
        unique_products_count = cursor.fetchone()[0]
        print(f"Total Unique Products in Database: {unique_products_count}")

        # Cross-check counts
        print("\nCross-check:")
        print(f"Products Fetched: {total_products}")
        print(f"Products Processed (Unique IDs): {len(processed_product_ids)}")
        print(f"Products Updated: {total_updated}")
        print(f"Products Skipped: {total_skipped}")

    except mysql.connector.Error as err:
        print(f"Error executing query: {err}")
    finally:
        print("\nSummary:")
        print(f"Total Products Found: {total_products}")
        print(f"Total Prices Updated: {total_updated}")
        print(f"Total Products Skipped: {total_skipped}")



def product_special_price_function(db, cursor):
    """Calculate and update special prices for products."""
    total_products = 0
    total_updated = 0
    total_skipped = 0

    try:
        # Query to fetch products
        query = "SELECT product_id, product_name, product_special_price, product_cost FROM products"
        print(f"Executing Query: {query}")
        cursor.execute(query)
        rows = cursor.fetchall()

        # Log the total rows fetched
        total_products = len(rows)
        print(f"Total Products Fetched: {total_products}")

        for row in rows:
            product_id, product_name, product_special_price, product_cost = row

            # Log product details for debugging
            print(f"Processing Product: ID={product_id}, Name={product_name}, Cost={product_cost}")

            if product_cost is None:
                print(f"Skipping Product: {product_name} (Cost is None)")
                total_skipped += 1
                continue

            # Calculate special price
            product_special_price = product_cost * 0.20 + 20 + product_cost

            # Update the product special price in the database
            try:
                cursor.execute(
                    "UPDATE products SET product_special_price = %s WHERE product_id = %s",
                    (product_special_price, product_id)
                )
                db.commit()
                total_updated += 1  # Increment updated products count
                print(f"Updated Product: ID={product_id}, New Special Price={product_special_price}")
            except mysql.connector.Error as update_err:
                print(f"Failed to Update Product: ID={product_id}, Error={update_err}")

        # Cross-check counts
        print("\nCross-check:")
        print(f"Products Fetched: {total_products}")
        print(f"Products Updated: {total_updated}")
        print(f"Products Skipped: {total_skipped}")

    except mysql.connector.Error as err:
        print(f"Error executing query: {err}")
    finally:
        print("\nSummary:")
        print(f"Total Products Found: {total_products}")
        print(f"Total Special Prices Updated: {total_updated}")
        print(f"Total Products Skipped: {total_skipped}")




def update_product_price(db, cursor):
    """Update product prices based on product_cost * 7."""
    total_products = 0
    total_processed = 0
    total_updated = 0
    total_skipped = 0

    try:
        # Query to fetch product details
        query = "SELECT product_id, product_name, product_price, product_cost FROM products"
        print(f"Executing Query: {query}")
        cursor.execute(query)
        rows = cursor.fetchall()

        # Total products fetched
        total_products = len(rows)
        print(f"Total Products Fetched: {total_products}")

        for row in rows:
            product_id, product_name, product_price, product_cost = row

            # Log product details
            print(f"Processing Product: ID={product_id}, Name={product_name}, Cost={product_cost}")

            # Check if product_cost is NULL
            if product_cost is None:
                print(f"Skipping Product: {product_name} (Cost is None)")
                total_skipped += 1
                continue

            # Calculate new product price
            new_product_price = product_cost * 7

            try:
                # Update the product price in the database
                cursor.execute(
                    "UPDATE products SET product_price = %s WHERE product_id = %s",
                    (new_product_price, product_id)
                )
                db.commit()
                total_updated += 1  # Increment updated count
                print(f"Updated Product: ID={product_id}, New Price={new_product_price}")
            except mysql.connector.Error as update_err:
                print(f"Failed to Update Product: ID={product_id}, Error={update_err}")

            total_processed += 1  # Increment processed count

        # Cross-check counts
        print("\nCross-check:")
        print(f"Products Fetched: {total_products}")
        print(f"Products Processed: {total_processed}")
        print(f"Products Updated: {total_updated}")
        print(f"Products Skipped (Null Cost): {total_skipped}")

    except mysql.connector.Error as err:
        print(f"Error executing query: {err}")
    finally:
        print("\nSummary:")
        print(f"Total Products Found: {total_products}")
        print(f"Total Products Processed: {total_processed}")
        print(f"Total Products Updated: {total_updated}")
        print(f"Total Products Skipped (Null Cost): {total_skipped}")




def main(db_config):
    db = connect_to_database(db_config)
    if db is None:
       return

    cursor = db.cursor()
    options = {
     '1' : lambda db=db, cursor=cursor: price_cost_function_loader(db,cursor),
     '2' : lambda db=db, cursor=cursor: product_special_price_function(db,cursor),
     '3' : lambda db=db, cursor=cursor: update_product_price(db,cursor)
    }

    while True:
       print("1. Load a price function - Market Price Change ")
       print("2. Load a price function - Product Special Price Change")
       print("3. Load a price function - Market Price Change 7 Cost fn")

       choice = input("Select an operation fucntion or 'exit' to exit").strip()

       if choice.lower() =='exit':
           break
 
       action = options.get(choice)
       if action:
           action()
       else:
           print("Invalid choice, please make the correct choice")

    cursor.close()
    db.close()

if __name__ == "__main__":
    main(db_config)
