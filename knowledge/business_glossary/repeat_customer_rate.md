# Business Term: Repeat Customer Rate

_Last generated: 2026-07-06 13:11:09_

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
| 100 | 0 |  |
| HUB_OLIST_ORDER | ORDER_ID |  |

### Transformation (plain language)

1. Carries the original order purchase timestamp directly from the SAP field ORDER_PURCHASE_TIMESTAMP, flowing through staging, vault, and mart layers unchanged to support monthly bucketing and repeat customer rate calculations.
2. Carries the stable person-level identifier sourced directly from SAP field CUSTOMER_UNIQUE_ID, flowing through staging, vault, and mart layers unchanged to support repeat-customer detection across multiple orders.
3. Counts the total number of unique orders placed, used as the basis for calculating the repeat customer rate.
   - *Join:* Primary table; one row per order. Joined to customers via customer_id (per_record_key, avg 1.00x per DAR-00959)
   - *Filter:* Exclude canceled orders (order_status NOT IN ('canceled','unavailable')). See warning on analyst filter decision blocker.
4. Truncates the order purchase timestamp to the first day of its calendar month, providing a standardized month-level date for grouping repeat customer metrics.
   - *Join:* Column on orders; no additional join needed
   - *Filter:* Zero nulls confirmed (DAR-00931: null_pct=0.0, spans 2016-09-04 to 2018-10-17). No COALESCE needed.
5. fact_repeat_customer_rate.repeat_customer_rate_pct carries the repeat customer rate percentage as a direct copy of the SAP source field 100.0, flowing through staging, vault, and mart layers without modification.
6. This column carries the raw order identifier flowing through unchanged from the SAP source field ORDER\_ID, used to count repeat orders when calculating the repeat customer rate.

### SQL (from dbt models)

**fact_repeat_customer_rate.total_orders:**
```sql
COUNT(DISTINCT order_id)
```

**fact_repeat_customer_rate.order_month:**
```sql
DATE_TRUNC('month', o.order_purchase_timestamp)::DATE
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
