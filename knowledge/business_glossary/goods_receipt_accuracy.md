# Business Term: Goods Receipt Accuracy

_Last generated: 2026-07-04 01:42:32_

## Definition

Percentage of goods receipt postings where received quantity matches PO ordered quantity (no partial or over-delivery), per vendor per quarter

- **ID:** `BG009`
- **Owner:** Warehouse Management
- **Approved by:** Head of Logistics
- **Status:** `draft`
- **Unit:** percent
- **Grain:** vendor × quarter
- **Domain:** quality
- **Related terms:** [on_time_delivery_rate](on_time_delivery_rate.md) · [vendor_scorecard](vendor_scorecard.md)

**Notes:** Partial deliveries (GR qty < PO qty) and over-deliveries both count as inaccurate.

## Source-to-Target Mapping

_(no S2T mapping defined yet)_

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

_(lineage will be documented when dbt models are built)_

## Validation Status

DRAFT — awaiting business owner approval

## Related Decisions (1)

- **#96** (2026-05-05) — rule3_enforcement_checker_plus_two_mart_refactors: RULE 3 now has a checker. Two of three violators refactored to vault-sourced patterns matching fact_purchase_orders / fact_invoices conventions. The third is allowlisted with a clear fix path (KI-121: build sat_invoice_item + zmm_approval_log staging+vault, then refactor). Future model changes that introduce new RULE 3 violations will hard-fail end_of_task.py.
