# Business Term: Olist On-Time Delivery Rate

_Last generated: 2026-07-06 19:02:51_

## Definition

Share of delivered orders where the actual customer delivery date is on or before the estimated delivery date promised at purchase, per month of order purchase. Orders not delivered or missing an actual delivery date are excluded from numerator and denominator.

- **ID:** `BG032`
- **Owner:** Analytics Team
- **Approved by:** mm
- **Status:** `approved`
- **Unit:** Percent
- **Grain:** month
- **Domain:** ecommerce_sales

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| ORDERS | ORDER_ID | Single source of all required fields: order_id (grain key), order_purchase_timestamp (month bucketing), order_status (filter to 'delivered'), order_delivered_customer_date (actual delivery date, numerator condition), and order_estimated_delivery_date (promised date at purchase, comparison target). The term definition is fully self-contained within this table — no joins are required. |
| orders (via sat_olist_order_header) | order_purchase_timestamp | Purchase timestamp; zero nulls, spans 2016-09-04 to 2018-10-17 (DAR-01024). Monthly grain derived via DATE_TRUNC. |
| SAT_OLIST_ORDER_HEADER | ORDER_DELIVERED_CUSTOMER_DATE |  |
| SAT_OLIST_ORDER_HEADER | ORDER_DELIVERED_CUSTOMER_DATE |  |

### Transformation (plain language)

1. Carries the raw order identifier from SAP field ORDERS.ORDER_ID as a direct pass-through through all pipeline layers, serving as the grain key that anchors each record used to calculate on-time delivery rate from order status, actual delivery date, and estimated delivery date within the same table.
2. Truncates the order purchase timestamp to the first day of its calendar month, grouping orders into monthly buckets for on-time delivery analysis.
   - *Join:* Attribute column on sat_olist_order_header
   - *Filter:* Not filtered; used as GROUP BY key
3. Counts the total number of delivered orders recorded for the month.
4. Counts the total number of orders that were delivered to the customer on time within each monthly period.
5. Calculates the percentage of orders delivered on time within each monthly period by dividing the count of on-time deliveries by the total number of orders, expressed as a percentage rounded to four decimal places.

### SQL (from dbt models)

**fact_on_time_delivery_monthly.order_month:**
```sql
DATE_TRUNC('month', order_purchase_timestamp)
```

**fact_on_time_delivery_monthly.delivered_orders:**
```sql
COUNT(*)
```

**fact_on_time_delivery_monthly.on_time_orders:**
```sql
SUM(is_on_time)
```

**fact_on_time_delivery_monthly.on_time_delivery_rate_pct:**
```sql
ROUND(
        100.0 * SUM(is_on_time) / NULLIF(COUNT(*), 0),
        4
    )
```

### Target Models

- `fact_on_time_delivery_monthly`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-07-04)

## Related Decisions (1)

- **#128** (2026-07-06) — freshness_gate_source_scoped_and_warns_not_blocks: Staleness is information for the analyst, not a lock. Scoping stays interim until source_system is first-class across the knowledge graph (consolidated catalog). Approval form also warns when no deployed S2T exists (a term approved before its S2T silently skipped the stage in the clean-room run).

## Open Issues (1)

- **#135** [open/low] Deploy step d.5 semantic gate reads OBT metadata before the view materializes — During BG032's UI deploy, step d.5 logged 'metadata read failed: Catalog Error: Table obt_ecommerce_on_time_delivery does not exist' for the OBT while validating it - the gate ran a beat before dbt created the view; it degraded gracefully (warn) and dbt test subsequently passed. …
