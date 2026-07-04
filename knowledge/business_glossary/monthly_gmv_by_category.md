# Business Term: Monthly GMV by Product Category

_Last generated: 2026-07-04 02:20:08_

## Definition

Gross merchandise value: sum of item price (excluding freight) for orders that are not canceled, per English product category per month of order purchase. Products without a category are grouped as 'uncategorized'.

- **ID:** `BG031`
- **Owner:** Analytics Team
- **Approved by:** 
- **Status:** `scope_confirmed`
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
| orders | order_status | Order lifecycle status (delivered, shipped, canceled, unavailable, invoiced, processing, created, approved) |
| order_items | price | Item price in BRL (excludes freight) |
| order_items | order_id | FK to orders — used to join items to order header for status filter and timestamp |
| order_items | product_id | FK to products — used to resolve category |
| products | product_category_name | Category in Portuguese; nullable (~610 products uncategorized per DAR-00981) |
| category_translation | product_category_name_english | English category name — target label for grouping |

### Transformation (plain language)

1. Extract year and month to form the reporting period bucket (YYYY-MM). Used to assign each order line item to a calendar month.
   - *Join:* orders is joined to order_items on order_id (header_detail, safe direction order_items->orders per DAR-00962, avg 1.14 items/order). orders is used solely to supply the purchase timestamp and the cancellation filter — it does not drive the aggregation grain.
   - *Filter:* WHERE order_status != 'canceled' — confirmed valid by DAR-00973 code_tables analysis (625 canceled rows out of 99,441 total; order_status has zero nulls per DAR-00970)
2. Used as a filter predicate only. Exclude rows where order_status = 'canceled'. DAR-00973 confirms 8 distinct values; 625 rows are 'canceled' (~0.63% of 99,441 orders). Zero nulls confirmed by DAR-00970.
   - *Join:* Filter applied on orders before joining to order_items.
   - *Filter:* Exclude canceled orders. All other statuses (delivered, shipped, unavailable, invoiced, processing, created, approved) are included.
3. SUM(price) across all non-canceled order items within a category-month bucket. DAR-00974 confirms zero nulls across all 112,650 rows and DAR-00978 shows avg=120.65 BRL, p25=39.9, p75=134.9. The PK (order_id, order_item_id) is confirmed at confidence=1.0 by DAR-00910, so no deduplication is needed before summing.
   - *Join:* order_items is the fact grain. Each row is one item line. Joined to orders on order_id to apply the status filter (header_detail per DAR-00962). Joined to products on product_id (safe direction order_items->products, catastrophic fanout warning applies only to reverse direction per DAR-00963).
   - *Filter:* Inherit the order_status != 'canceled' filter from orders join. No additional price filter — zero nulls confirmed.
4. Join key from order_items to orders. Part of PK composite (order_id, order_item_id). Used to bring in order_purchase_timestamp and order_status.
   - *Join:* header_detail join (order_items->orders). Safe direction confirmed by DAR-00962 (avg 1.14x fanout). orders joined only for filter and timestamp — not for aggregation.
   - *Filter:* No filter on this field itself.
5. Join key from order_items to products (safe direction per DAR-00963). 32,951 distinct product_ids in order_items; 100% RI to products confirmed by DAR-00910.
   - *Join:* LEFT JOIN order_items -> products on product_id. Safe join direction (order_items->products). The reverse (products->order_items) is catastrophic_fanout (DAR-00963, avg 4.13x). LEFT JOIN preserves items whose product has no category.
   - *Filter:* No filter on product_id itself.
6. Join key from products to category_translation. Empirically confirmed null_count=610 (null_pct=1.85%) by DAR-00981. When NULL, the final COALESCE maps to 'uncategorized'. LEFT JOIN to category_translation preserves these rows.
   - *Join:* LEFT JOIN products -> category_translation on product_category_name. Safe direction (products->category_translation) per DAR-00958 cardinality note (reverse direction is catastrophic_fanout avg 443x). category_translation has 73 rows, 0 nulls per DAR-00992.
   - *Filter:* No filter — NULLs are preserved and later mapped to 'uncategorized'.
7. Used as the category dimension after LEFT JOIN from products. DAR-00992 confirms 0 nulls in category_translation itself (73 rows, 73 distinct English names per DAR-00993). When a product has no Portuguese category (610 products), product_category_name_english will be NULL after the LEFT JOIN — COALESCE to 'uncategorized'.
   - *Join:* Resolved via products LEFT JOIN category_translation on product_category_name. The join key in category_translation has 0 nulls and 73 distinct values (DAR-00993). All 73 Portuguese keys match at 1.0 ratio (DAR-00958).
   - *Filter:* No filter — COALESCE handles the NULL case for uncategorized products.

### SQL (from dbt models)

**fact_gmv_by_category_monthly.order_month:**
```sql
DATE_TRUNC('month', CAST(order_purchase_timestamp AS TIMESTAMP))
```

**fact_gmv_by_category_monthly.(filter only — not projected):**
```sql
WHERE order_status != 'canceled'
```

**fact_gmv_by_category_monthly.gmv_brl:**
```sql
SUM(oi.price)
```

**fact_gmv_by_category_monthly.(join key — not projected):**
```sql
oi.order_id = o.order_id
```

**fact_gmv_by_category_monthly.(join key — not projected):**
```sql
oi.product_id = p.product_id
```

**fact_gmv_by_category_monthly.(join key — not projected):**
```sql
p.product_category_name = ct.product_category_name
```

**fact_gmv_by_category_monthly.product_category_english:**
```sql
COALESCE(ct.product_category_name_english, 'uncategorized')
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

Status: `scope_confirmed`

## Related Decisions (3)

- **#123** (2026-06-29) **[NEVER_REPEAT]** — enforce_rule3_and_vault_columns_at_generation: Enforce architecture rules at GENERATION with deterministic pre-flights + bounded repair-retry; dbt build is the hard backstop.
- **#124** (2026-07-04) — second_source_experiment_olist_proves_agnostic_mechanism: The mechanism generalizes. Source onboarding = load schema + dictionary rows + run analyzers. Olist demo models live under dbt/models/olist behind DG_ENABLE_OLIST.
- **#126** (2026-07-04) — greenfield_source_generation_contracts: Every generation-time contract needs a defined greenfield behavior. Grounding must cover everything the model is allowed to ref().
