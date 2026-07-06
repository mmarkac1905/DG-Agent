# Business Term: Delivery Status Distribution

_Last generated: 2026-07-06 13:11:09_

## Definition

Classification of PO items by fulfillment state: pending (no GR posted yet), partially_received (GR qty < PO qty), fully_received (GR qty >= PO qty)

- **ID:** `BG013`
- **Owner:** Analytics Team
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** count per status
- **Grain:** po_item
- **Domain:** procurement
- **Related terms:** [avg_vendor_lead_time](avg_vendor_lead_time.md) · [on_time_delivery_rate](on_time_delivery_rate.md)

**Notes:** Computed at PO item grain by comparing EKPO.MENGE against aggregated EKBE GR quantities.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EKPO | MENGE | PO ordered quantity |
| EKBE | MENGE | GR posted quantity from PO history |

### Transformation (plain language)

1. This column categorizes each purchase order line item's delivery status as "pending" when no goods receipts exist, "fully_received" when the total received quantity equals or exceeds the ordered quantity, or "partially_received" when some but not all ordered quantity has been received.
   - *Join:* Join to EKBE on EBELN+EBELP
   - *Filter:* Filter EKBE.VGABE='1' (goods receipt postings)
2. This column shows the total quantity of goods received for each purchase order, defaulting to zero when no goods receipt records exist.
   - *Join:* EKBE one row per GR event
   - *Filter:* VGABE='1' for GR events

### SQL (from dbt models)

**fact_purchase_orders.delivery_status:**
```sql
CASE
        WHEN fgr.first_gr_date IS NULL THEN 'pending'
        WHEN gr_totals.total_gr_quantity >= pi.ordered_quantity THEN 'fully_received'
        WHEN gr_totals.total_gr_quantity > 0 THEN 'partially_received'
        ELSE 'pending'
    END
```

**fact_purchase_orders.received_quantity:**
```sql
COALESCE(gr_totals.total_gr_quantity, 0)
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
