# Business Term: CPE Lifecycle Status

_Last generated: 2026-07-07 12:28:57_

## Definition

Current status of an individual CPE device: in_stock (received not deployed) \| deployed (issued to customer) \| returned (back from customer) \| defective (failed QA or returned defective) \| scrapped (end of life)

- **ID:** `BG006`
- **Owner:** Asset Management
- **Approved by:** Head of Technical Operations
- **Status:** `approved`
- **Unit:** enum
- **Grain:** equipment (serial number)
- **Domain:** equipment
- **Related terms:** [cpe_defect_rate](cpe_defect_rate.md)

**Notes:** Derived from latest movement type in MSEG + equipment status in EQBS.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EQUI | EQUNR | Equipment number |
| EQBS | USTXT | Status description |

### Transformation (plain language)

1. This column carries the equipment number directly from SAP field EQUI.EQUNR through all pipeline layers unchanged to serve as the primary identifier for equipment records.
   - *Filter:* All equipment
2. The lifecycle_status column maps SAP equipment status codes to business-friendly labels, such as 'INST' to 'deployed' and 'AVLB' to 'in_stock', with any unmapped codes defaulting to 'unknown'.
   - *Filter:* Latest status by timestamp

### SQL (from dbt models)

**dim_equipment.lifecycle_status:**
```sql
CASE
        WHEN ls.status_code = 'INST' THEN 'deployed'
        WHEN ls.status_code = 'AVLB' THEN 'in_stock'
        WHEN ls.status_code = 'RET' THEN 'returned'
        WHEN ls.status_code = 'DLFL' THEN 'defective'
        ELSE 'unknown'
    END
```

### Target Models

- `dim_equipment`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-13)
