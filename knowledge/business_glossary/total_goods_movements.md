# Business Term: Total Goods Movements

_Last generated: 2026-07-06 10:08:36_

## Definition

Count of material document items (all movement types: GR, GI, transfers, returns, reversals)

- **ID:** `BG023`
- **Owner:** Analytics Team
- **Approved by:** Head of Logistics
- **Status:** `approved`
- **Unit:** count
- **Grain:** period
- **Domain:** inventory
- **Related terms:** [total_purchase_orders](total_purchase_orders.md) · [deployment_return_ratio](deployment_return_ratio.md)

**Notes:** Volume metric for warehouse activity. Includes all BWART codes.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MSEG | MBLNR | Material document number |

### Transformation (plain language)

1. This column carries the material document number that flows through unchanged from the SAP MSEG.MBLNR field, serving as the unique identifier for each goods movement transaction.
   - *Join:* One row per movement line
   - *Filter:* All BWART codes included

### SQL (from dbt models)

### Target Models

- `fact_goods_movements`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-14)

## Related Decisions (1)

- **#67** (2026-04-18) **[NEVER_REPEAT]** — fix_b_reverted_archive_is_final: Archive is final — once a term is archived, its analysis_findings stay with the archived term_id as audit trail and do not follow a re-created same-named term. A re-created term starts with zero findings and must run fresh Guided Analysis. This produces a clean per-term-id semantic that matches how s2t_mapping and domain_facts already behave. See RULE 42 revision #5. Cross-term data bleeding was causing more bugs than it solved (decision #66 hotfix 8 caught the first; hotfix 8 extension caught the second; a third surfaced with analyst confusion about which term "owned" a profiling run).
