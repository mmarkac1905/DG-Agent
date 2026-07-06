# Business Term: CPE Return Rate

_Last generated: 2026-07-07 01:07:16_

## Definition

Percentage of CPE devices that have been returned from customer (status RET), per equipment category

- **ID:** `BG019`
- **Owner:** Analytics Team
- **Approved by:** Head of Technical Operations
- **Status:** `approved`
- **Unit:** percent
- **Grain:** equipment_category
- **Domain:** equipment
- **Related terms:** [cpe_defect_rate](cpe_defect_rate.md) · [cpe_lifecycle_status](cpe_lifecycle_status.md)

**Notes:** Counts movement type 161 receipts. Excludes vendor returns (mvt 122) which are tracked under defect rate.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EQBS | USTXT | Equipment user status |

### Transformation (plain language)

1. The return rate percentage is calculated by dividing the number of items with lifecycle status 'returned' by the total number of items and multiplying by 100, rounded to two decimal places.
   - *Join:* Latest status per equipment
   - *Filter:* Latest status only

### SQL (from dbt models)

**knowledge_cpe_lifecycle_metrics.return_rate_pct:**
```sql
ROUND(100.0 * COUNT(CASE WHEN e.lifecycle_status = 'returned' THEN 1 END) / COUNT(*), 2)
```

### Target Models

- `knowledge_cpe_lifecycle_metrics`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-14)
