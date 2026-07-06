# Business Term: Zero-Stock Locations

_Last generated: 2026-07-06 19:02:51_

## Definition

Count of material x plant x storage location combinations where unrestricted stock = 0. Indicates potential stockout risk

- **ID:** `BG021`
- **Owner:** Analytics Team
- **Approved by:** Head of Logistics
- **Status:** `approved`
- **Unit:** count
- **Grain:** total
- **Domain:** inventory
- **Related terms:** [days_of_stock](days_of_stock.md) · [total_stock_units](total_stock_units.md)

**Notes:** Alert metric. Should be 0 for critical CPE during business hours; non-zero triggers reorder review.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MARD | LABST | Unrestricted stock quantity |

### Transformation (plain language)

1. This flag indicates whether a storage location has zero or negative unrestricted stock quantity.
   - *Join:* Snapshot from MARD
   - *Filter:* Excludes blocked-only or QI-only locations

### SQL (from dbt models)

**fact_inventory.is_zero_stock:**
```sql
CASE WHEN sl.unrestricted_stock <= 0 THEN TRUE ELSE FALSE END
```

### Target Models

- `fact_inventory`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-14)
