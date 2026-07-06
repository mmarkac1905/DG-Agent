# Business Term: Vendor Spend Share

_Last generated: 2026-07-06 19:02:51_

## Definition

Percentage of total procurement spend attributed to a single vendor in a given quarter

- **ID:** `BG014`
- **Owner:** Analytics Team
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** percent
- **Grain:** vendor x quarter
- **Domain:** procurement
- **Related terms:** [vendor_concentration_risk](vendor_concentration_risk.md) · [total_po_value](total_po_value.md)

**Notes:** Computed in obt_vendor_scorecard. Threshold for concentration risk = 60% per PR004.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EKKO | LIFNR | Vendor account number |

### Transformation (plain language)

1. This column calculates each vendor's percentage share of total quarterly spend by dividing the vendor's quarterly spend by the overall quarterly spend total, rounded to four decimal places, or zero if there was no total spend in the quarter.
   - *Join:* Join EKKO to EKPO on EBELN
   - *Filter:* All BSTYP='F' POs

### SQL (from dbt models)

**obt_vendor_scorecard.vendor_spend_share:**
```sql
CASE WHEN qt.quarter_total_spend > 0
        THEN ROUND(vq.total_spend / qt.quarter_total_spend, 4)
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
