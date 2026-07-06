# Business Term: PO Cycle Time

_Last generated: 2026-07-06 13:11:09_

## Definition

Calendar days from purchase requisition creation (EBAN.BADAT) to purchase order creation (EKKO.BEDAT), measuring internal procurement processing speed

- **ID:** `BG008`
- **Owner:** Procurement Department
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** days
- **Grain:** purchasing_group × month
- **Domain:** procurement
- **Related terms:** [avg_vendor_lead_time](avg_vendor_lead_time.md)

**Notes:** Measures INTERNAL efficiency not vendor delivery. Target: <5 days for standard reorders.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EBAN | BADAT | PR creation date |
| EKKO | BEDAT | PO creation date |

### Transformation (plain language)

1. This column contains the purchase requisition date from the SAP purchase requisition document, converted to a standard date format.
   - *Join:* EBAN.BANFN = EKPO.BANFN
   - *Filter:* Only PRs that converted to POs
2. The number of days between the purchase requisition date and the purchase order creation date, calculated only when both dates are available.
   - *Filter:* Exclude auto-created PRs from MRP (EBAN.ESTKZ = 'B')

### SQL (from dbt models)

**fact_purchase_orders.pr_date:**
```sql
CAST(BADAT AS DATE)
```

**fact_purchase_orders.po_cycle_days:**
```sql
CASE WHEN prd.requisition_date IS NOT NULL AND ph.po_date IS NOT NULL
        THEN CAST(ph.po_date - prd.requisition_date AS INTEGER)
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
