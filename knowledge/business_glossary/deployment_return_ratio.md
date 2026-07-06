# Business Term: Deployment to Return Ratio

_Last generated: 2026-07-06 19:29:00_

## Definition

Ratio of customer returns (mvt 161) to deployments (mvt 201) per equipment category. Lower is better — indicates installation quality and product reliability

- **ID:** `BG024`
- **Owner:** Analytics Team
- **Approved by:** Head of Technical Operations
- **Status:** `approved`
- **Unit:** ratio
- **Grain:** equipment_category
- **Domain:** equipment
- **Related terms:** [cpe_return_rate](cpe_return_rate.md) · [cpe_deployment_rate](cpe_deployment_rate.md)

**Notes:** Should be below 0.05 (5 percent) for healthy CPE categories. Higher values trigger field operations review.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MSEG | BWART | Movement type code |

### Transformation (plain language)

1. This ratio compares the total quantity of goods returned from deployment (movement type 161) to the total quantity of goods issued to deployment (movement type 201).
   - *Join:* Group by material group
   - *Filter:* BWART IN ('161','201')

### SQL (from dbt models)

**obt_goods_movements.signed_quantity:**
```sql
SUM(qty WHERE bwart='161') / SUM(qty WHERE bwart='201')
```

### Target Models

- `obt_goods_movements`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-14)
