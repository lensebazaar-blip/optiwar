import re
import os
import unicodedata
import MySQLdb
import MySQLdb.cursors
from MySQLdb import Error

def slugify(text):
    """Convert text to a URL-friendly slug."""
    try:
        # Normalize unicode characters and convert to ASCII
        text = str(text)  # Ensure text is string type
        text = unicodedata.normalize('NFKD', text)  # Fixed typo: NKFD -> NFKD
        text = text.encode('ascii', 'ignore').decode('ascii')
        # Remove special characters, convert to lowercase
        text = re.sub(r'[^\w\s-]', '', text).strip().lower()
        # Replace spaces and underscores with single hyphens
        return re.sub(r'[\s_-]+', '-', text)
    except (TypeError, AttributeError) as e:
        print(f"Error processing text '{text}': {str(e)}")
        return ""

def get_db_connection():
    """Create and return a database connection."""
    try:
        return MySQLdb.connect(
            host='localhost',
            user='oslb6',
            password=os.environ.get('MYSQL_PASSWORD', ''),
            database='optiwar2',
            cursorclass=MySQLdb.cursors.DictCursor
        )
    except Error as e:
        print(f"Database connection error: {str(e)}")
        raise

def main():
    try:
        # Establish database connection
        db = get_db_connection()
        cursor = db.cursor()

        # Fetch products
        cursor.execute('SELECT product_id, product_name FROM products')
        products = cursor.fetchall()

        # Update slugs
        for product in products:
            slug = slugify(product['product_name'])
            cursor.execute(
                'UPDATE products SET product_slug = %s WHERE product_id = %s',
                (slug, product['product_id'])
            )

        # Commit changes
        db.commit()
        print(f"Successfully updated {len(products)} product slugs.")

    except Error as e:
        print(f"Database error: {str(e)}")
        if 'db' in locals():
            db.rollback()
    finally:
        if 'db' in locals():
            db.close()

if __name__ == '__main__':
    main()
