# Business Term: Total Stock Units

_Last generated: 2026-07-07 01:07:16_

## Definition

Sum of unrestricted + quality inspection + blocked stock across all plants and storage locations

- **ID:** `BG020`
- **Owner:** Analytics Team
- **Approved by:** Head of Logistics
- **Status:** `approved`
- **Unit:** count (ST)
- **Grain:** total or per material per plant
- **Domain:** inventory
- **Related terms:** [days_of_stock](days_of_stock.md) · [blocked_stock](blocked_stock.md)

**Notes:** Snapshot from MARD. Includes all stock types — for unrestricted-only see days_of_stock.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MARD | LABST | Unrestricted stock quantity |

### Transformation (plain language)

1. The total stock is the sum of unrestricted stock, quality inspection stock, and blocked stock quantities, converted to a decimal format with three decimal places.
   - *Join:* Snapshot from MARD
   - *Filter:* Includes all material groups

### SQL (from dbt models)

**fact_inventory.total_stock:**
```sql
CAST(sl.unrestricted_stock + sl.quality_inspection_stock + sl.blocked_stock AS DECIMAL(13, 3))
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
