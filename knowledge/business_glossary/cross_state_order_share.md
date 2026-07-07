# Business Term: Cross-State Order Share

_Last generated: 2026-07-07 11:33:06_

## Definition

Share of delivered orders where at least one item was fulfilled by a seller located in a different state than the customer, per month of order purchase. An order counts as cross-state if any of its items' seller    state differs from the customer state. Orders that are not delivered are excluded.

- **ID:** `BG035`
- **Owner:** 
- **Approved by:** Martin Markac
- **Status:** `approved`
- **Unit:** percent
- **Grain:** month
- **Domain:** ecommerce_sales

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| ORDERS | ORDER_ID | Order header table providing order_status (to filter for delivered orders only) and order_purchase_timestamp (to derive the month grain for the metric). |
| ORDER_ITEMS | SELLER_ID | Order line table linking each order to its seller via seller_id; required to compare seller state against customer state at item level, and to apply the 'any item cross-state' logic per order. |
| CUSTOMERS | CUSTOMER_STATE | Customer master providing customer_state (2-letter BR state code); joined to orders via customer_id to get the customer's state for each order. |
| SELLERS | SELLER_STATE | Seller master providing seller_state (2-letter BR state code); joined to order_items via seller_id to get the fulfilling seller's state for each order line. |
| orders | order_id | Unique identifier of the order (PK of orders table) |
| orders | order_purchase_timestamp | Timestamp when the order was placed. DAR-01024: min=2016-09-04, max=2018-10-17, null_pct=0.0. |
| HUB_OLIST_ORDER | ORDER_ID |  |
| HUB_OLIST_ORDER | ORDER_ID |  |
| HUB_OLIST_ORDER | ORDER_ID |  |

### Transformation (plain language)

1. Carries the raw order identifier flows through unchanged from ORDERS.ORDER_ID, serving as the foundational key linking order status and purchase timestamp details used to calculate the share of cross-state orders among delivered transactions within a given month.
2. Carries the raw seller identifier directly from ORDER_ITEMS.SELLER_ID unchanged through all pipeline layers, enabling cross-state comparisons between seller and customer locations at the order item level.
3. This column carries the customer's 2-letter Brazilian state code as a direct pass-through of the raw source field CUSTOMER_STATE from the CUSTOMERS master table, joined to each order via customer_id.
4. This column carries the seller's two-letter Brazilian state code as a direct pass-through of the raw source field SELLER_STATE from the SELLERS master table, flowing unchanged through all pipeline layers to support cross-state order share analysis.
5. The total number of unique orders delivered, counted without duplication across repeated records.
   - *Join:* Base table driving the metric. One row per order. Filtered to order_status = 'delivered'.
   - *Filter:* WHERE order_status = 'delivered'
6. Truncates the order purchase timestamp to the first day of its calendar month, converting it to a standard date, to identify which month each order belongs to.
   - *Join:* Header table — drives monthly bucketing of every order.
   - *Filter:* No additional filter; null_pct = 0.0 per DAR-01024.
7. This column carries the raw order identifier value flowing through unchanged from HUB_OLIST_ORDER.ORDER_ID, representing the cross-state order share percentage as sourced directly from the pipeline without modification.
8. Carries the raw order identifier directly from HUB_OLIST_ORDER.ORDER_ID, flowing through staging, vault, and mart layers unchanged to represent each cross-state order included in the share calculation.
9. This column carries a direct copy of the ORDER_ID field from HUB_OLIST_ORDER, flowing through staging, vault, and mart layers unchanged to represent the same-state order records contributing to the cross-state order share calculation.

### SQL (from dbt models)

**fact_cross_state_order_share.delivered_orders:**
```sql
COUNT(DISTINCT order_id)
```

**fact_cross_state_order_share.order_month:**
```sql
DATE_TRUNC('month', order_purchase_timestamp)::DATE
```

### Target Models

- `fact_cross_state_order_share`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-07-06)
