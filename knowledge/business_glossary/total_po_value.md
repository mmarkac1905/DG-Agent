# Business Term: Total PO Value

_Last generated: 2026-07-07 11:22:47_

## Definition

Total net value (EUR) of all PO line items in the selected period

- **ID:** `BG012`
- **Owner:** Analytics Team
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** EUR
- **Grain:** period
- **Domain:** procurement
- **Related terms:** [total_purchase_orders](total_purchase_orders.md) · [vendor_concentration_risk](vendor_concentration_risk.md)

**Notes:** Sum of EKPO.NETWR. Excludes invoice corrections.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EKPO | NETWR | PO line item net value |

### Transformation (plain language)

1. This column represents the total net value of all purchase order line items by summing the net worth amounts from the purchasing document items.
   - *Join:* PO item grain — multiple items per PO

### SQL (from dbt models)

**fact_purchase_orders.net_value:**
```sql
SUM(NETWR)
```

### Target Models

- `fact_purchase_orders`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-14)
