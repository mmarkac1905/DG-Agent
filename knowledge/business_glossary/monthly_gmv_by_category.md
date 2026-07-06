# Business Term: Monthly GMV by Product Category

_Last generated: 2026-07-06 19:11:41_

## Definition

Gross merchandise value: sum of item price (excluding freight) for orders that are not canceled, per English product category per month of order purchase. Products without a category are grouped as 'uncategorized'.

- **ID:** `BG031`
- **Owner:** Analytics Team
- **Approved by:** mm
- **Status:** `approved`
- **Unit:** BRL
- **Grain:** product_category x month
- **Domain:** ecommerce_sales

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| ORDERS | ORDER_PURCHASE_TIMESTAMP | Carries order_purchase_timestamp (month dimension) and order_status (needed to filter out canceled orders); grain anchor for the non-canceled order filter. |
| ORDER_ITEMS | PRICE | Carries price (the BRL measure, explicitly excluding freight per definition) and product_id (FK to products); one row per item, summed to produce GMV. |
| PRODUCTS | PRODUCT_CATEGORY_NAME | Carries product_category_name (Portuguese) and product_id; the LEFT JOIN source that enables the 'uncategorized' grouping when product_category_name IS NULL. |
| CATEGORY_TRANSLATION | PRODUCT_CATEGORY_NAME_ENGLISH | Translates product_category_name (Portuguese) to product_category_name_english; required by the definition's 'per English product category' grain. |
| orders | order_purchase_timestamp | Purchase timestamp (when the order was placed) |
| order_items | price | Item price in BRL (excludes freight) |
| category_translation | product_category_name_english | English category name — target label for grouping |
| LINK_OLIST_ORDER_ITEM | ORDER_ID |  |

### Transformation (plain language)

1. Carries the order purchase timestamp directly from ORDERS.ORDER_PURCHASE_TIMESTAMP through staging, vault, and mart layers unchanged, serving as the month-level grain anchor used to attribute gross merchandise value to its corresponding category and period.
2. Carries the per-item transaction price directly from the SAP field ORDER_ITEMS.PRICE, flowing through staging, vault, and mart layers unchanged to support monthly gross merchandise value aggregation by category.
3. Carries the product category name directly from SAP field PRODUCT_CATEGORY_NAME through all pipeline layers unchanged, enabling grouping of sales by category — including an 'uncategorized' bucket when no category is assigned.
4. Carries the English-language product category name directly from SAP field CATEGORY_TRANSLATION.PRODUCT_CATEGORY_NAME_ENGLISH, enabling gross merchandise value reporting at the per-English-product-category grain.
5. Truncates the order purchase timestamp to the first day of its calendar month, grouping all orders into monthly periods for aggregation.
   - *Join:* orders is joined to order_items on order_id (header_detail, safe direction order_items->orders per DAR-00962, avg 1.14 items/order). orders is used solely to supply the purchase timestamp and the cancellation filter — it does not drive the aggregation grain.
   - *Filter:* WHERE order_status != 'canceled' — confirmed valid by DAR-00973 code_tables analysis (625 canceled rows out of 99,441 total; order_status has zero nulls per DAR-00970)
6. The total gross merchandise value in BRL for each category and month, calculated by summing the price of all order items within that grouping.
   - *Join:* order_items is the fact grain. Each row is one item line. Joined to orders on order_id to apply the status filter (header_detail per DAR-00962). Joined to products on product_id (safe direction order_items->products, catastrophic fanout warning applies only to reverse direction per DAR-00963).
   - *Filter:* Inherit the order_status != 'canceled' filter from orders join. No additional price filter — zero nulls confirmed.
7. The English product category name sourced from the category translation reference table.
   - *Join:* Resolved via products LEFT JOIN category_translation on product_category_name. The join key in category_translation has 0 nulls and 73 distinct values (DAR-00993). All 73 Portuguese keys match at 1.0 ratio (DAR-00958).
   - *Filter:* No filter — COALESCE handles the NULL case for uncategorized products.
8. Counts the total number of line items included in the monthly GMV aggregation for each category.
9. Counts the number of unique orders included in each monthly category record.

### SQL (from dbt models)

**fact_gmv_by_category_monthly.order_month:**
```sql
DATE_TRUNC('month', order_purchase_timestamp)
```

**fact_gmv_by_category_monthly.gmv_brl:**
```sql
SUM(price)
```

**fact_gmv_by_category_monthly.product_category_english:**
```sql
product_category_english
```

**fact_gmv_by_category_monthly.item_count:**
```sql
COUNT(*)
```

**fact_gmv_by_category_monthly.order_count:**
```sql
COUNT(DISTINCT order_id)
```

### Target Models

- `fact_gmv_by_category_monthly`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-07-04)

## Related Decisions (3)

- **#123** (2026-06-29) **[NEVER_REPEAT]** — enforce_rule3_and_vault_columns_at_generation: Enforce architecture rules at GENERATION with deterministic pre-flights + bounded repair-retry; dbt build is the hard backstop.
- **#124** (2026-07-04) — second_source_experiment_olist_proves_agnostic_mechanism: The mechanism generalizes. Source onboarding = load schema + dictionary rows + run analyzers. Olist demo models live under dbt/models/olist behind DG_ENABLE_OLIST.
- **#126** (2026-07-04) — greenfield_source_generation_contracts: Every generation-time contract needs a defined greenfield behavior. Grounding must cover everything the model is allowed to ref().
