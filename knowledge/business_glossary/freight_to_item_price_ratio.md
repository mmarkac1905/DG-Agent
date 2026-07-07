# Business Term: Freight-to-Item Price Ratio

_Last generated: 2026-07-07 12:28:57_

## Definition

The ratio of total freight value to total item price (excluding freight) for all order line items in delivered orders, expressed as a percentage, per English product category per calendar month of order purchase. Total freight value is the sum of freight_value across all items; total item price is the sum of price across the same items. Only order line items belonging to orders with status 'delivered' are included. Categories are expressed in English.

- **ID:** `BG036`
- **Owner:** 
- **Approved by:** mm
- **Status:** `approved`
- **Unit:** percentage (%)
- **Grain:** English product category × calendar month of order purchase
- **Domain:** logistics

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| ORDER_ITEMS | PRICE | Carries the two core measures — freight_value and price — at the order line grain; PK is (order_id, order_item_id), which defines the item-level unit of analysis for both the numerator (sum of freight_value) and denominator (sum of price). |
| ORDERS | ORDER_STATUS | Provides order_status (needed to filter to 'delivered' orders) and order_purchase_timestamp (needed to derive the calendar month dimension of the grain). |
| PRODUCTS | PRODUCT_CATEGORY_NAME | Bridges order_items.product_id to category_translation via product_category_name; also carries product_category_name in Portuguese, which is the join key into the translation table. |
| CATEGORY_TRANSLATION | PRODUCT_CATEGORY_NAME_ENGLISH | Translates product_category_name (Portuguese) to product_category_name_english, which is required by the grain definition ('English product category'). |
| order_items | freight_value | Item freight value in BRL (order freight is split across items) |
| order_items | price | Item price in BRL (excludes freight) |
| orders | order_purchase_timestamp | Purchase timestamp; truncated to calendar month for grain |
| category_translation | product_category_name_english | English category label used as the category grain dimension |
| SAT_OLIST_ORDER_ITEM_PRICES | FREIGHT_VALUE |  |
| LINK_OLIST_ORDER_ITEM | HK_OLIST_ORDER_ITEM |  |
| LINK_OLIST_ORDER_ITEM | HK_OLIST_ORDER |  |

### Transformation (plain language)

1. Carries the item unit price unchanged from ORDER_ITEMS.PRICE, serving as the denominator in the freight-to-item-price ratio calculation at the order line grain.
2. This column carries the raw order status value and purchase timestamp details as a direct copy of the ORDERS.ORDER_STATUS source field, flowing through all pipeline layers unchanged to support delivered-order filtering and monthly grain derivation for the freight-to-item price ratio metric.
3. This column carries the raw product category name in Portuguese directly from PRODUCTS.PRODUCT\_CATEGORY\_NAME, flowing through staging, vault, and mart layers unchanged as the join key to the category translation table.
4. Carries the English translation of the product category name as a direct pass-through from CATEGORY_TRANSLATION.PRODUCT_CATEGORY_NAME_ENGLISH, flowing unchanged through all pipeline layers.
5. The total freight charges (in Brazilian Reais) summed across all line items within the grouping, representing the aggregated freight cost for the order or segment being analyzed.
   - *Join:* Sourced from sat_olist_order_item_prices via hk_olist_order_item; joined to link_olist_order_item to get order_id and product_id, then to sat_olist_order_header for status filter and sat_olist_product_category for category label.
   - *Filter:* Only items in delivered orders (order_status = 'delivered')
6. The total item price in Brazilian Reais, calculated by summing the individual item prices across all order line items.
   - *Join:* Sourced from sat_olist_order_item_prices via hk_olist_order_item; same join path as freight_value.
   - *Filter:* Only items in delivered orders (order_status = 'delivered')
7. The calendar month in which the order was placed, taken directly from the order purchase timestamp.
   - *Join:* Sourced from sat_olist_order_header via hk_olist_order.
   - *Filter:* All delivered orders; timestamp is non-null (DAR-01020)
8. The English product category name sourced from the category translation table, defaulting to "uncategorized" when no translation is available.
   - *Join:* Sourced directly from sat_olist_product_category.product_category_english — the vault satellite already stores the English translation, so no separate join to a translation table is needed at the mart layer.
   - *Filter:* NULLs coalesced to 'uncategorized'
9. Carries the raw freight-to-item price ratio percentage as a direct copy of the FREIGHT_VALUE field from SAT_OLIST_ORDER_ITEM_PRICES, flowing through staging, vault, and mart layers without modification.
10. This column carries the raw order item identifier directly from LINK_OLIST_ORDER_ITEM.HK_OLIST_ORDER_ITEM, flowing through staging, vault, and mart layers unchanged to represent the count of items associated with each freight-to-price ratio calculation.
11. Direct copy of HK_OLIST_ORDER from LINK_OLIST_ORDER_ITEM, carrying the unique order identifier used to count the number of orders contributing to the freight-to-item price ratio calculation.

### SQL (from dbt models)

**fact_freight_to_price_ratio.total_freight_value_brl:**
```sql
SUM(freight_value)
```

**fact_freight_to_price_ratio.total_item_price_brl:**
```sql
SUM(price)
```

**fact_freight_to_price_ratio.order_month:**
```sql
order_month
```

**fact_freight_to_price_ratio.product_category_english:**
```sql
COALESCE(pc.product_category_english, 'uncategorized')
```

### Target Models

- `fact_freight_to_price_ratio`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-07-07)
