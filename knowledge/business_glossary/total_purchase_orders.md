# Business Term: Total Purchase Orders

_Last generated: 2026-07-07 12:05:18_

## Definition

Count of distinct purchase order documents in the selected period

- **ID:** `BG011`
- **Owner:** Analytics Team
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** count
- **Grain:** period
- **Domain:** procurement
- **Related terms:** [avg_vendor_lead_time](avg_vendor_lead_time.md) · [total_po_value](total_po_value.md)

**Notes:** Volume metric. Counts unique EBELN values from EKKO; respects active filters.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EKKO | EBELN | Purchase order document number |

### Transformation (plain language)

1. This column carries the purchase order document number as a direct copy of the SAP EKKO.EBELN field, flowing through unchanged from the source system.
   - *Join:* One row per PO header
   - *Filter:* BSTYP='F' (standard purchase orders only)

### SQL (from dbt models)

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

## Related Decisions (2)

- **#15** (2026-04-14) — full_glossary_alignment: Self-documenting data product complete. Any business user can hover any KPI and see definition + source tables + transformation in one tooltip. Honest sample data caveat in sidebar.
- **#110** (2026-05-14) **[NEVER_REPEAT]** — ki71_strict_cascade_archive_with_block_if_shared: Archive refuses rather than half-cleans. Three blocker classes - sharing (s2t_mapping ownership), downstream (manifest reverse-refs), in-flight (re-entry guard). Guided unwind tracks the chain in session_state and records blockers_resolved on the terminal archive_log row. Already-archived terms hit an idempotent no-op path. Manifest freshness gate runs dbt compile only when stale (mtime + model-presence checks). Reference impl: app/archive_dependency_analyzer.analyze_archive_impact + app/archive_term.run_archive.
