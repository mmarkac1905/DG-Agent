# Business Term: CPE Defect Rate

_Last generated: 2026-07-07 12:28:57_

## Definition

Percentage of CPE devices returned to vendor (movement type 122) within 90 days of goods receipt, per material per vendor per quarter

- **ID:** `BG003`
- **Owner:** Quality Management
- **Approved by:** Head of Technical Operations
- **Status:** `approved`
- **Unit:** percent
- **Grain:** material × vendor × quarter
- **Domain:** quality
- **Related terms:** [vendor_scorecard](vendor_scorecard.md) · [cpe_lifecycle_status](cpe_lifecycle_status.md)

**Notes:** Only counts returns within 90d of GR. Excludes customer-initiated returns (mvt 161).

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MSEG | BWART | Movement type |
| EQUI | ERDAT | Equipment receipt date |

### Transformation (plain language)

1. This column converts SAP movement type codes into descriptive business categories, such as mapping code '101' to 'goods_receipt' and code '102' to 'gr_reversal', with any unmapped codes defaulting to 'other'.
   - *Filter:* Category 'vendor_return' isolates CPEs returned to the supplier — the numerator for cpe_defect_rate
2. This column calculates the number of days between the equipment's creation date and today's date, returning null if no creation date exists.
   - *Join:* dim_equipment materializes days_since_receipt directly from EQUI
   - *Filter:* All equipment

### SQL (from dbt models)

**fact_goods_movements.movement_category:**
```sql
CASE
        WHEN gri.movement_type = '101' THEN 'goods_receipt'
        WHEN gri.movement_type = '102' THEN 'gr_reversal'
        WHEN gri.movement_type = '122' THEN 'vendor_return'
        WHEN gri.movement_type = '161' THEN 'customer_return'
        WHEN gri.movement_type = '201' THEN 'deployment'
        WHEN gri.movement_type = '202' THEN 'gi_reversal'
        WHEN gri.movement_type = '301' THEN 'plant_transfer'
        WHEN gri.movement_type = '561' THEN 'initial_stock'
        ELSE 'other'
    END
```

**dim_equipment.days_since_receipt:**
```sql
CASE WHEN g.created_date IS NOT NULL
        THEN CAST(CURRENT_DATE - g.created_date AS INTEGER)
        ELSE NULL
    END
```

### Target Models

- `dim_equipment`
- `fact_goods_movements`

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

- **#108** (2026-05-12) **[NEVER_REPEAT]** — vendor_country_appears_not_to_affect_defect_rate_but_query_was_broken: DO NOT use obt_procurement_overview alone for vendor attribution of equipment outcomes — the material_number join is many-to-many across vendors. Correct path: traverse link_equipment_gr -> link_gr_po -> link_po_vendor in the vault, or build a fact_equipment_with_vendor mart that follows that chain. Migrated from decommissioned signal_relationships #6.
