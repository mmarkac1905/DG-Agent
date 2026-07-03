# Business Term: PO Volume by Month

_Last generated: 2026-07-04 01:42:32_

## Definition

Count of distinct purchase orders created per calendar month. Used for volume trend analysis and seasonality detection

- **ID:** `BG025`
- **Owner:** Analytics Team
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** count
- **Grain:** month
- **Domain:** procurement
- **Related terms:** [total_purchase_orders](total_purchase_orders.md) · [purchase_order_cycle_time](purchase_order_cycle_time.md)

**Notes:** No strong seasonality observed in current data — see signal_relationships #7.

## Source-to-Target Mapping

### Source Tables (SAP)

| Table | Field | Description |
| --- | --- | --- |
| EKKO | BEDAT | PO creation date |

### Transformation (plain language)

1. The column counts the number of unique purchase orders created in each year-month period, derived by grouping purchase order document numbers by the first 7 characters of the document date (YYYY-MM format).
   - *Join:* One row per PO header
   - *Filter:* All BSTYP='F'

### SQL (from dbt models)

**obt_procurement_overview.po_year_month:**
```sql
COUNT(DISTINCT EBELN) GROUP BY SUBSTR(BEDAT,1,7)
```

### Target Models

- `obt_procurement_overview`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-14)

## Related Decisions (1)

- **#15** (2026-04-14) — full_glossary_alignment: Self-documenting data product complete. Any business user can hover any KPI and see definition + source tables + transformation in one tooltip. Honest sample data caveat in sidebar.
