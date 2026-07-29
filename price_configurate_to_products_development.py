import MySQLdb
import os
import csv

def get_db_connection():
    return MySQLdb.connect(
        host="localhost",
        user="oslb6",
        passwd=os.environ.get('MYSQL_PASSWORD', ''),
        db="optiwar2"
    )


def main():
    # Step 1: Connect and fetch data
    conn = get_db_connection()
    cursor = conn.cursor(MySQLdb.cursors.DictCursor)
    cursor.execute("SELECT product_code, product_id, product_cost FROM products WHERE product_cost IS NOT NULL")
    products = cursor.fetchall()

    # Step 2: Get user inputs
    try:
        a_cost = float(input("Enter a_cost: "))
        b_cost = float(input("Enter b_cost: "))
        c_cost = float(input("Enter c_cost: "))
        loader_of = float(input("Enter loader_of (prev 550): "))
        decision_cost = float(input("Enter decision_cost (prev 250): "))
    except ValueError:
        print("Invalid input. Please enter numeric values.")
        return

    configurate_cost = a_cost + b_cost + c_cost

    # Step 3: Process each product
    results = []
    for product in products:
        product_code = product.get('product_code',None)
        product_id = product["product_id"]
        product_cost = float(product["product_cost"])

        configurate_total = configurate_cost + product_cost
        try:
            result_of_configurate_cost_by_product_cost = configurate_cost / product_cost
        except ZeroDivisionError:
            result_of_configurate_cost_by_product_cost = 0

        try:
            result_of_loader_configurate_cost_by_product_cost = loader_of / result_of_configurate_cost_by_product_cost
        except ZeroDivisionError:
            result_of_loader_configurate_cost_by_product_cost = 0

        if result_of_loader_configurate_cost_by_product_cost >= decision_cost:
            adder_forward = 60
        else:
            adder_forward = result_of_loader_configurate_cost_by_product_cost

        final_cost = round(product_cost + configurate_cost + adder_forward, 2)

        results.append({
            "product_code": product_code,
            "product_id": product_id,
            "product_cost": product_cost,
            "final_cost": final_cost
        })

    # Step 4: Show planned updates
    print(f"\n{len(results)} entries will be updated in 'product_special_price':")
    for res in results:
        print(f"Product Code: {res['product_code']} || Product ID: {res['product_id']} | Old Cost: {res['product_cost']} | New Special Price: {res['final_cost']}")

    # Step 5: Confirm and update
    confirm = input("\nType 'yes' to proceed with update in database: ").strip().lower()
    if confirm in ("yes", "y"):
        update_cursor = conn.cursor()
        for res in results:
            update_cursor.execute(
                "UPDATE products SET product_special_price = %s WHERE product_id = %s",
                (res["final_cost"], res["product_id"])
            )
        conn.commit()
        print(f"\n✅ {len(results)} rows updated successfully.")
    else:
        print("\n❌ Update cancelled.")

    # Step 6: Optional CSV export
    export = input("Do you want to export the new prices to CSV? (yes/no): ").strip().lower()
    if export in ("yes", "y"):
        with open("updated_configurated_products.csv", "w", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=["product_id", "product_cost", "final_cost"])
            writer.writeheader()
            writer.writerows(results)
        print("📁 CSV exported as 'updated_configurated_products.csv'.")
    else:
        print("Skipped CSV export.")

    cursor.close()
    conn.close()

if __name__ == "__main__":
    main()

