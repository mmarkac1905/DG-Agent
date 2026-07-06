# Business Term: Inventory Turnover Ratio

_Last generated: 2026-07-06 19:11:41_

## Definition

Number of times average CPE inventory is sold/deployed in a period. Calculated as total goods issues (mvt 201) divided by average stock level, per material group per quarter

- **ID:** `BG004`
- **Owner:** Warehouse Management
- **Approved by:** Head of Logistics
- **Status:** `approved`
- **Unit:** ratio
- **Grain:** material_group × quarter
- **Domain:** inventory
- **Related terms:** [days_of_stock](days_of_stock.md) · [reorder_point](reorder_point.md)

**Notes:** Higher = better. Target varies by CPE type: routers >4x/year ONTs >3x/year STBs >2x/year.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MARD | LABST | Unrestricted stock |
| MSEG | MENGE | Issued quantity |

### Transformation (plain language)

1. This column represents the unrestricted stock quantity from the material master plant data, converted to a decimal format with 3 decimal places for precise inventory calculations.
   - *Filter:* Exclude blocked and QI stock
2. This column shows the positive quantity for goods receipts (movement types 101, 161, 202, 561) and negative quantity for goods issues (movement types 102, 122, 201), with all other movement types set to zero.
   - *Filter:* Movement type 201 = goods issue to cost center

### SQL (from dbt models)

**fact_inventory.unrestricted_stock:**
```sql
CAST(LABST AS DECIMAL(13,3))
```

**fact_goods_movements.signed_quantity:**
```sql
CASE
        WHEN gri.movement_type IN ('101', '161', '202', '561') THEN gri.quantity
        WHEN gri.movement_type IN ('102', '122', '201') THEN -1 * gri.quantity
        ELSE 0
    END
```

### Target Models

- `fact_goods_movements`
- `fact_inventory`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-13)
