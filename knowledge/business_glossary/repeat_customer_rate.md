# Business Term: Repeat Customer Rate

_Last generated: 2026-07-04 02:20:08_

## Definition

Share of orders placed by a repeat customer, per month of order purchase. A repeat customer is a person who has placed at least one earlier order at any prior time; person identity must be the stable customer identifier, not the order-scoped key.

- **ID:** `BG033`
- **Owner:** Analytics Team
- **Approved by:** 
- **Status:** `scope_confirmed`
- **Unit:** Percent
- **Grain:** month
- **Domain:** ecommerce_sales

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| ORDERS | ORDER_PURCHASE_TIMESTAMP | Fact table carrying one row per order with order_purchase_timestamp (used to derive the month grain) and customer_id (FK to customers); provides the denominator (all orders) and the date dimension for monthly bucketing. |
| CUSTOMERS | CUSTOMER_UNIQUE_ID | Carries customer_unique_id — the stable person-level identifier required by the definition to distinguish the same person across multiple orders; customer_id is order-scoped and cannot be used for repeat-customer detection without this table. |
| orders | order_id | Unique identifier of the order — used as the unit of counting for numerator and denominator |
| orders | order_purchase_timestamp | Purchase timestamp — used to derive order month and to determine order sequence per person |
| orders | order_status | Order lifecycle status — used to filter in-scope orders for denominator and numerator |
| customers | customer_id | Order-scoped customer key — used only to JOIN orders to customers; NOT used for repeat detection |
| customers | customer_unique_id | Stable person identifier — the ONLY correct key for identifying repeat customers across orders. customer_id is order-scoped and MUST NOT be used for repeat detection. |

### Transformation (plain language)

1. Count distinct order_id values for denominator (all orders in month); count only those where the person had a prior order for numerator
   - *Join:* Primary table; one row per order. Joined to customers via customer_id (per_record_key, avg 1.00x per DAR-00959)
   - *Filter:* Exclude canceled orders (order_status NOT IN ('canceled','unavailable')). See warning on analyst filter decision blocker.
2. Truncate to YYYY-MM for monthly grain; also used in window function to detect prior orders for the same customer_unique_id
   - *Join:* Column on orders; no additional join needed
   - *Filter:* Zero nulls confirmed (DAR-00931: null_pct=0.0, spans 2016-09-04 to 2018-10-17). No COALESCE needed.
3. Exclude 'canceled' and 'unavailable' statuses from both numerator and denominator. DAR-00973 confirms: delivered=96,478, shipped=1,107, canceled=625, unavailable=609, invoiced=314, processing=301, created=5, approved=2. Zero nulls (DAR-00970).
   - *Join:* Column on orders; no additional join
   - *Filter:* Exclude canceled (625) and unavailable (609) — these represent orders that were never fulfilled. Delivered+shipped+invoiced+processing+created+approved are included as 'placed' orders.
4. Join key between orders.customer_id and customers.customer_id. Per DAR-00959 this is a per_record_key join (avg 1.00x fanout, 100% matched). Per DAR-00996 zero nulls.
   - *Join:* customers joined to orders via customer_id; per_record_key (DAR-00959). RI=100% (DAR-00908).
   - *Filter:* Zero nulls on both sides (DAR-00996, DAR-00970); no null filter needed
5. Use in LAG/window function to detect whether a person placed a prior order. Group by customer_unique_id and order by order_purchase_timestamp; if MIN(order_purchase_timestamp) over the person < current order's timestamp, the order is a repeat order. Zero nulls confirmed (DAR-00996).
   - *Join:* Reached via customers table after joining orders.customer_id -> customers.customer_id
   - *Filter:* Zero nulls (DAR-00996). All 99,441 rows have a non-null customer_unique_id.

### SQL (from dbt models)

**fact_repeat_customer_rate.total_orders:**
```sql
COUNT(DISTINCT o.order_id)
```

**fact_repeat_customer_rate.order_month:**
```sql
DATE_TRUNC('month', o.order_purchase_timestamp)::DATE AS order_month
```

**fact_repeat_customer_rate.order_status_filter:**
```sql
WHERE o.order_status NOT IN ('canceled', 'unavailable')
```

**fact_repeat_customer_rate.join_key_only:**
```sql
JOIN customers c ON o.customer_id = c.customer_id
```

**fact_repeat_customer_rate.is_repeat_order:**
```sql
MIN(o.order_purchase_timestamp) OVER (PARTITION BY c.customer_unique_id) AS first_order_ts
```

### Target Models

- `fact_repeat_customer_rate`

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

- **#124** (2026-07-04) — second_source_experiment_olist_proves_agnostic_mechanism: The mechanism generalizes. Source onboarding = load schema + dictionary rows + run analyzers. Olist demo models live under dbt/models/olist behind DG_ENABLE_OLIST.
- **#126** (2026-07-04) — greenfield_source_generation_contracts: Every generation-time contract needs a defined greenfield behavior. Grounding must cover everything the model is allowed to ref().
- **#127** (2026-07-04) — blind_definition_probe_bg034_customer_key: Honest claim: the trap is resolved from CATALOG DOCUMENTATION + PROFILED EVIDENCE, not from the term definition - and not from data alone either (the dictionary rows carry the public dataset docs). Reading the catalog correctly is the product working as designed; claiming blind discovery would overstate it. The hard stop shows the convergence gate is stricter than the key choice.
