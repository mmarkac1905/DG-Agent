# Business Term: Blocked Stock

_Last generated: 2026-07-06 19:02:51_

## Definition

Total quantity of stock in blocked status across all locations. Blocked stock cannot be used until quality review is completed

- **ID:** `BG022`
- **Owner:** Analytics Team
- **Approved by:** Head of Logistics
- **Status:** `approved`
- **Unit:** count (ST)
- **Grain:** total or per material
- **Domain:** inventory
- **Related terms:** [total_stock_units](total_stock_units.md) · [cpe_defect_rate](cpe_defect_rate.md)

**Notes:** Quality flag. Spikes indicate potential vendor quality issues; review with vendor scorecard.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MARD | SPEME | Blocked stock quantity |

### Transformation (plain language)

1. The blocked stock represents the total quantity of materials that are blocked from use, summed across all plant-material combinations from the MARD table's blocked stock field.
   - *Join:* Snapshot from MARD
   - *Filter:* Quality review required before use

### SQL (from dbt models)

**fact_inventory.blocked_stock:**
```sql
SUM(SPEME)
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
