"""Generate data-dictionary rows for the Olist e-commerce source.

Second-source experiment: appends per-column documentation for the
raw_olist tables into dbt/seeds/sap_data_dictionary.csv so Stage A
scope derivation can reason about the source. Types/lengths/examples
come from the live DuckDB schema; descriptions come from the public
Kaggle dataset documentation (Brazilian E-Commerce Public Dataset by
Olist). Idempotent: re-running replaces the Olist rows in place.
"""
import csv
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DICT_CSV = ROOT / "dbt" / "seeds" / "sap_data_dictionary.csv"
DB = ROOT / "cpe_analytics.duckdb"
SCHEMA = "raw_olist"

# Column documentation from the public Kaggle dataset description.
DESCR: dict[str, dict[str, str]] = {
    "orders": {
        "_table": "Order header: one row per order.",
        "order_id": "Unique identifier of the order.",
        "customer_id": "Key to the customers table. IMPORTANT: unique per order — each order has its own customer_id; use customers.customer_unique_id to identify the same person across orders.",
        "order_status": "Order status lifecycle: delivered, shipped, canceled, unavailable, invoiced, processing, created, approved.",
        "order_purchase_timestamp": "Purchase timestamp (when the order was placed).",
        "order_approved_at": "Payment approval timestamp. Nullable.",
        "order_delivered_carrier_date": "Order posting timestamp — handed to the logistic partner. Nullable.",
        "order_delivered_customer_date": "Actual order delivery date to the customer. Nullable (not yet delivered / data gaps: a few delivered orders lack it).",
        "order_estimated_delivery_date": "Estimated delivery date shown to the customer at purchase.",
    },
    "order_items": {
        "_table": "Order line items: one row per item within an order; an order can have multiple items.",
        "order_id": "Order this item belongs to (FK to orders).",
        "order_item_id": "Sequential number of the item within the order (1..N). PK is (order_id, order_item_id).",
        "product_id": "Product ordered (FK to products).",
        "seller_id": "Seller fulfilling this item (FK to sellers).",
        "shipping_limit_date": "Seller's shipping deadline for handing the item to the carrier.",
        "price": "Item price in BRL (excludes freight).",
        "freight_value": "Item freight value in BRL (order freight is split across items).",
    },
    "order_payments": {
        "_table": "Order payment records: one row per payment method/installment sequence — an order can have MULTIPLE payment rows (vouchers, split payments). Joining orders to payments multiplies order rows.",
        "order_id": "Order the payment belongs to (FK to orders).",
        "payment_sequential": "Sequence number of the payment for the order (1..N).",
        "payment_type": "Payment method: credit_card, boleto, voucher, debit_card, not_defined.",
        "payment_installments": "Number of installments chosen by the customer.",
        "payment_value": "Transaction value in BRL. SUM over an order's rows = order total paid (including freight).",
    },
    "order_reviews": {
        "_table": "Post-purchase customer reviews. Mostly one per order, but review_id values can repeat (one review covering multiple orders) — deduplicate before counting.",
        "review_id": "Review identifier. NOT unique — the same review_id can appear for multiple orders.",
        "order_id": "Order being reviewed (FK to orders).",
        "review_score": "1-5 satisfaction score.",
        "review_comment_title": "Review title in Portuguese. Mostly null.",
        "review_comment_message": "Review free-text in Portuguese. Often null.",
        "review_creation_date": "Date the review survey was sent.",
        "review_answer_timestamp": "Timestamp the customer answered.",
    },
    "customers": {
        "_table": "Customer records at ORDER grain: one row per order's customer key.",
        "customer_id": "Key referenced by orders. Unique per order, NOT per person.",
        "customer_unique_id": "Stable identifier of the person — use this to find repeat customers across orders.",
        "customer_zip_code_prefix": "First 5 digits of the customer zip code. Joins to geolocation.geolocation_zip_code_prefix (many rows per prefix!).",
        "customer_city": "Customer city name.",
        "customer_state": "Customer state (2-letter BR code).",
    },
    "sellers": {
        "_table": "Marketplace sellers: one row per seller.",
        "seller_id": "Unique seller identifier.",
        "seller_zip_code_prefix": "First 5 digits of the seller zip code. Joins to geolocation.geolocation_zip_code_prefix (many rows per prefix!).",
        "seller_city": "Seller city name.",
        "seller_state": "Seller state (2-letter BR code).",
    },
    "products": {
        "_table": "Product catalog: one row per product.",
        "product_id": "Unique product identifier.",
        "product_category_name": "Category in Portuguese; decode to English via category_translation. Nullable (~610 products uncategorized).",
        "product_name_lenght": "Length of the product name in characters (source column name misspelled in the original dataset).",
        "product_description_lenght": "Length of the product description (misspelled in the original dataset).",
        "product_photos_qty": "Number of product photos.",
        "product_weight_g": "Product weight in grams.",
        "product_length_cm": "Package length in cm.",
        "product_height_cm": "Package height in cm.",
        "product_width_cm": "Package width in cm.",
    },
    "category_translation": {
        "_table": "Decoder table: product category Portuguese -> English. Join products N:1 to this (safe lookup direction).",
        "product_category_name": "Category name in Portuguese (join key from products).",
        "product_category_name_english": "Category name in English.",
    },
    "geolocation": {
        "_table": "Zip-prefix geolocation points: MANY rows per zip prefix (up to ~1,100). Joining on zip prefix without aggregation catastrophically multiplies rows — aggregate to one point per prefix first.",
        "geolocation_zip_code_prefix": "First 5 digits of zip code. NOT unique.",
        "geolocation_lat": "Latitude of one observed point in the prefix.",
        "geolocation_lng": "Longitude of one observed point in the prefix.",
        "geolocation_city": "City name.",
        "geolocation_state": "State (2-letter BR code).",
    },
}


def main() -> int:
    con = duckdb.connect(str(DB), read_only=True)
    live = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema='{SCHEMA}' ORDER BY table_name, ordinal_position"
    ).fetchall()

    new_rows = []
    for table, col, dtype in live:
        tdoc = DESCR.get(table, {})
        desc = tdoc.get(col, "(no public documentation for this column)")
        table_ctx = tdoc.get("_table", "")
        example = con.execute(
            f'SELECT "{col}" FROM {SCHEMA}."{table}" WHERE "{col}" IS NOT NULL LIMIT 1'
        ).fetchone()
        new_rows.append({
            "table_name": table,
            "field_name": col,
            "data_type": dtype,
            "length": "",
            "description_en": desc,
            "description_hr": "",
            "business_meaning": (table_ctx + " " + desc).strip(),
            "example_value": "" if example is None else str(example[0])[:60],
            "domain_area": "ecommerce_sales",
            "description_source": "olist_kaggle_documentation",
            "needs_review": "0",
        })
    con.close()

    with open(DICT_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        kept = [r for r in reader if r["description_source"] != "olist_kaggle_documentation"]

    with open(DICT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in kept + new_rows:
            w.writerow(r)

    print(f"sap_data_dictionary.csv: kept {len(kept)} existing rows, "
          f"wrote {len(new_rows)} olist rows ({len(set(r['table_name'] for r in new_rows))} tables)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
