# Business Term: Days of Stock (DOS)

_Last generated: 2026-07-07 11:00:29_

## Definition

Average number of days current stock level would last based on trailing 30-day average daily consumption (goods issues mvt 201)

- **ID:** `BG005`
- **Owner:** Warehouse Management
- **Approved by:** Head of Logistics
- **Status:** `approved`
- **Unit:** days
- **Grain:** material × plant
- **Domain:** inventory
- **Related terms:** [inventory_turnover_ratio](inventory_turnover_ratio.md) · [reorder_point](reorder_point.md)

**Notes:** Below safety stock threshold (14 days) triggers reorder alert.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MARD | LABST | Current stock |
| MSEG | MENGE | Daily consumption |

### Transformation (plain language)

1. The total stock is the sum of unrestricted stock, quality inspection stock, and blocked stock from the material master, converted to a decimal format.
   - *Filter:* Non-negative only
2. The column represents the average quantity of goods issued to cost centers (movement type 201) across all relevant transactions.
   - *Filter:* Movement type 201 only

### SQL (from dbt models)

**obt_inventory_health.total_stock:**
```sql
CAST(LABST + INSME + SPEME AS DECIMAL(13,3))
```

**fact_goods_movements.quantity:**
```sql
AVG(quantity) FILTER (WHERE movement_type='201')
```

### Target Models

- `fact_goods_movements`
- `obt_inventory_health`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-13)

## Related Decisions (1)

- **#11** (2026-04-14) — knowledge_models_built: Intelligence layer complete. Session startup now surfaces live system state with health assessments. Context export ready for chat sessions.
