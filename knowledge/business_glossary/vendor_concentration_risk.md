# Business Term: Vendor Concentration Risk

_Last generated: 2026-07-07 11:00:29_

## Definition

Percentage of total CPE procurement spend going to a single vendor, measured quarterly. Above 60% = high risk per dual-sourcing policy (PR004)

- **ID:** `BG010`
- **Owner:** Risk Management
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** percent
- **Grain:** vendor × quarter
- **Domain:** procurement
- **Related terms:** [vendor_scorecard](vendor_scorecard.md)

**Notes:** Procurement rule PR004 requires 2+ approved vendors per critical CPE type.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EKPO | NETWR | PO item net value |

### Transformation (plain language)

1. This column calculates each vendor's share of total quarterly spend as a percentage, dividing the vendor's quarterly spending by the organization's total quarterly spending and rounding to four decimal places.
   - *Filter:* All completed POs

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

APPROVED — Business owner approved definition (2026-04-13)
