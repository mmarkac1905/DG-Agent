# Business Term: Monthly GMV by Product Category

_Last generated: 2026-07-04 01:42:32_

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

### Source Tables (SAP)

| Table | Field | Description |
| --- | --- | --- |
| ORDERS | ORDER_PURCHASE_TIMESTAMP | Carries order_purchase_timestamp (month dimension) and order_status (needed to filter out canceled orders); grain anchor for the non-canceled order filter. |
| ORDER_ITEMS | PRICE | Carries price (the BRL measure, explicitly excluding freight per definition) and product_id (FK to products); one row per item, summed to produce GMV. |
| PRODUCTS | PRODUCT_CATEGORY_NAME | Carries product_category_name (Portuguese) and product_id; the LEFT JOIN source that enables the 'uncategorized' grouping when product_category_name IS NULL. |
| CATEGORY_TRANSLATION | PRODUCT_CATEGORY_NAME_ENGLISH | Translates product_category_name (Portuguese) to product_category_name_english; required by the definition's 'per English product category' grain. |

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

## Related Decisions (3)

- **#123** (2026-06-29) **[NEVER_REPEAT]** — enforce_rule3_and_vault_columns_at_generation: Enforce architecture rules at GENERATION with deterministic pre-flights + bounded repair-retry; dbt build is the hard backstop.
- **#124** (2026-07-04) — second_source_experiment_olist_proves_agnostic_mechanism: The mechanism generalizes. Source onboarding = load schema + dictionary rows + run analyzers. Olist demo models live under dbt/models/olist behind DG_ENABLE_OLIST.
- **#126** (2026-07-04) — greenfield_source_generation_contracts: Every generation-time contract needs a defined greenfield behavior. Grounding must cover everything the model is allowed to ref().
