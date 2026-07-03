# Business Term: Repeat Customer Rate

_Last generated: 2026-07-04 01:42:32_

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

### Source Tables (SAP)

| Table | Field | Description |
| --- | --- | --- |
| ORDERS | ORDER_PURCHASE_TIMESTAMP | Fact table carrying one row per order with order_purchase_timestamp (used to derive the month grain) and customer_id (FK to customers); provides the denominator (all orders) and the date dimension for monthly bucketing. |
| CUSTOMERS | CUSTOMER_UNIQUE_ID | Carries customer_unique_id — the stable person-level identifier required by the definition to distinguish the same person across multiple orders; customer_id is order-scoped and cannot be used for repeat-customer detection without this table. |

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

## Related Decisions (2)

- **#124** (2026-07-04) — second_source_experiment_olist_proves_agnostic_mechanism: The mechanism generalizes. Source onboarding = load schema + dictionary rows + run analyzers. Olist demo models live under dbt/models/olist behind DG_ENABLE_OLIST.
- **#126** (2026-07-04) — greenfield_source_generation_contracts: Every generation-time contract needs a defined greenfield behavior. Grounding must cover everything the model is allowed to ref().

## Open Issues (1)

- **#134** [open/low] BG033 semantic validator warnings pending analyst review — fact_repeat_customer_rate passed semantic validation (match=true) with two warnings: (1) the first-order baseline excludes canceled/unavailable orders, so a person whose first-ever order was canceled is misclassified as first-time on their next order; (2) the mart also excludes s…
