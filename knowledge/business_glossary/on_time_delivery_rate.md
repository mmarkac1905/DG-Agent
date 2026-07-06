# Business Term: On-Time Delivery Rate

_Last generated: 2026-07-06 19:11:41_

## Definition

Percentage of purchase order items where goods receipt was posted within the agreed delivery date, per vendor per quarter

- **ID:** `BG002`
- **Owner:** Procurement Department
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** percent
- **Grain:** vendor × quarter
- **Domain:** procurement
- **Related terms:** [avg_vendor_lead_time](avg_vendor_lead_time.md) · [vendor_scorecard](vendor_scorecard.md)

**Notes:** Delivery date from EKET schedule line. Tolerance: +2 calendar days.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EKET | EINDT | Scheduled delivery date |
| MKPF | BUDAT | Actual GR date |

### Transformation (plain language)

1. This column represents the scheduled delivery date from the purchase order schedule line, converted from the original SAP date format to a standard date format.
   - *Join:* EKET.EBELN = EKKO.EBELN AND EKET.EBELP = EKPO.EBELP
   - *Filter:* Latest schedule line per PO item
2. This column indicates whether a purchase order was delivered on time by comparing the first goods receipt date against the scheduled delivery date plus a 2-day grace period, returning true if delivered within that window and null if either date is missing.
   - *Filter:* 2-day tolerance per business rule

### SQL (from dbt models)

**fact_purchase_orders.scheduled_delivery_date:**
```sql
CAST(EINDT AS DATE)
```

**fact_purchase_orders.is_on_time:**
```sql
CASE
        WHEN fgr.first_gr_date IS NOT NULL AND ps.scheduled_delivery_date IS NOT NULL
        THEN CASE WHEN fgr.first_gr_date <= ps.scheduled_delivery_date + INTERVAL 2 DAY
                  THEN TRUE ELSE FALSE END
        ELSE NULL
    END
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

APPROVED — Business owner approved definition (2026-04-13)

## Related Decisions (1)

- **#9** (2026-04-14) — marts_and_obt_built: Full analytical stack complete: raw -> staging -> vault -> marts -> OBT. Fixed three spec bugs: hk_po_item vs hk_po_material mismatch gr_totals hk_material_document join fact_invoices hk_vendor not in sat.
