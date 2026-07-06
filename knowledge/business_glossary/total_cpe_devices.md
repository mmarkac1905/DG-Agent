# Business Term: Total CPE Devices

_Last generated: 2026-07-06 19:02:51_

## Definition

Count of distinct equipment records (individual serialized CPE devices) currently tracked in the system

- **ID:** `BG017`
- **Owner:** Analytics Team
- **Approved by:** Head of Technical Operations
- **Status:** `approved`
- **Unit:** count
- **Grain:** total
- **Domain:** equipment
- **Related terms:** [cpe_lifecycle_status](cpe_lifecycle_status.md) · [cpe_deployment_rate](cpe_deployment_rate.md)

**Notes:** Counts EQUI.EQUNR. Includes all lifecycle statuses (in stock + deployed + returned + defective).

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EQUI | EQUNR | Equipment number (serial-tracked CPE device) |

### Transformation (plain language)

1. This column carries the equipment number that flows through unchanged from the SAP EQUI.EQUNR field, identifying each serial-tracked CPE device in the system.
   - *Join:* One row per CPE device
   - *Filter:* All equipment categories

### SQL (from dbt models)

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

APPROVED — Business owner approved definition (2026-04-14)
