# Business Term: Vendor Fulfillment Rate

_Last generated: 2026-07-07 11:22:47_

## Definition

Ratio of total received quantity to total ordered quantity per vendor over all time

- **ID:** `BG015`
- **Owner:** Analytics Team
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** percent
- **Grain:** vendor
- **Domain:** procurement
- **Related terms:** [on_time_delivery_rate](on_time_delivery_rate.md) · [goods_receipt_accuracy](goods_receipt_accuracy.md)

**Notes:** Reflects how completely a vendor delivers what was ordered. Lower than 100% means consistent under-delivery.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EKBE | MENGE | Total received quantity per vendor |

### Transformation (plain language)

1. The fulfillment rate represents the percentage of ordered quantity that was actually received from the vendor, calculated by dividing total received quantity by total ordered quantity and rounded to four decimal places, or zero if no quantity was ordered.
   - *Join:* Join EKBE→EKPO→EKKO on EBELN
   - *Filter:* VGABE='1'

### SQL (from dbt models)

**obt_vendor_scorecard.fulfillment_rate:**
```sql
CASE WHEN vq.total_ordered_qty > 0
        THEN ROUND(vq.total_received_qty / vq.total_ordered_qty, 4)
        ELSE 0
    END
```

### Target Models

- `obt_vendor_scorecard`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-14)
