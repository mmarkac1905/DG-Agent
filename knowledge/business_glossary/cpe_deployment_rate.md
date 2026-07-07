# Business Term: CPE Deployment Rate

_Last generated: 2026-07-07 11:33:06_

## Definition

Percentage of CPE devices currently in deployed (INST) lifecycle status, per equipment category

- **ID:** `BG018`
- **Owner:** Analytics Team
- **Approved by:** Head of Technical Operations
- **Status:** `approved`
- **Unit:** percent
- **Grain:** equipment_category
- **Domain:** equipment
- **Related terms:** [cpe_lifecycle_status](cpe_lifecycle_status.md) · [total_cpe_devices](total_cpe_devices.md)

**Notes:** Derived from latest equipment status in EQBS. Higher = better fleet utilization.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EQBS | USTXT | Equipment user status |

### Transformation (plain language)

1. The deployment rate percentage is calculated by dividing the count of equipment with 'deployed' lifecycle status by the total count of all equipment records, then multiplying by 100 and rounding to one decimal place.
   - *Join:* Latest status per equipment
   - *Filter:* Latest status only

### SQL (from dbt models)

**knowledge_cpe_lifecycle_metrics.deployment_rate_pct:**
```sql
ROUND(100.0 * COUNT(CASE WHEN e.lifecycle_status = 'deployed' THEN 1 END) / COUNT(*), 1)
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
