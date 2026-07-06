# Business Term: Repeat Customer Rate (blind-definition test)

_Last generated: 2026-07-06 10:08:36_

## Definition

Share of orders placed by a repeat customer, per month of order purchase. A repeat customer is a person who has placed at least one earlier order at any prior time.

- **ID:** `BG034`
- **Owner:** Analytics Team
- **Approved by:** 
- **Status:** `scope_confirmed`
- **Unit:** Percent
- **Grain:** month
- **Domain:** ecommerce_sales

**Notes:** Methodology probe: identical to BG033 but WITHOUT the definition-level hint about the stable customer identifier. Tests whether scope derivation and term analysis choose customer_unique_id from catalog+EDA evidence alone.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| ORDERS | ORDER_PURCHASE_TIMESTAMP | Orders table carries order_purchase_timestamp (the 'month of order purchase' grain dimension), order_id (the unit being counted as repeat or not), and customer_id (the FK used to join to customers for person-level identity resolution). |
| CUSTOMERS | CUSTOMER_UNIQUE_ID | Customers table carries customer_unique_id — the stable person-level identifier explicitly described in the catalog as 'use this to find repeat customers across orders', distinguishing it from customer_id which is unique per order. This field is the only available mechanism to group multiple orders to a single person and therefore determine whether any given order belongs to a repeat customer. |

### Transformation (plain language)


### SQL (from dbt models)


## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

Status: `scope_confirmed`

## Related Decisions (1)

- **#127** (2026-07-04) — blind_definition_probe_bg034_customer_key: Honest claim: the trap is resolved from CATALOG DOCUMENTATION + PROFILED EVIDENCE, not from the term definition - and not from data alone either (the dictionary rows carry the public dataset docs). Reading the catalog correctly is the product working as designed; claiming blind discovery would overstate it. The hard stop shows the convergence gate is stricter than the key choice.
