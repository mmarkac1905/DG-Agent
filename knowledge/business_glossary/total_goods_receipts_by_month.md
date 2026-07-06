# Business Term: Total Goods Receipts by Month

_Last generated: 2026-07-06 15:06:02_

## Definition

Count of goods receipt line items recorded into warehouse stock, aggregated by calendar month. Used for inflow tracking, vendor performance monitoring, and reconciliation against open purchase orders. Counts each line item on a goods receipt document — partial deliveries against the same PO each contribute their own line. Excludes reversals and corrections (cancelling movements that reverse a prior receipt).

- **ID:** `BG029`
- **Owner:** Warehouse Management
- **Approved by:** Head of Logistic
- **Status:** `ready_for_s2t`
- **Unit:** count
- **Grain:** month
- **Domain:** inventory
- **Related terms:** [total_goods_movements](total_goods_movements.md) · [po_volume_by_month](po_volume_by_month.md)

**Notes:** Volume metric for inbound warehouse activity. Counts line items rather than documents because a single goods receipt can cover multiple PO lines with different materials/quantities — line-item granularity reflects actual physical receipts. Reversals are excluded so the metric reflects net inflow. Compare against po_volume_by_month for receipts-vs-orders alignment.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| MSEG | MENGE | Material document line items — MSEG contains each goods receipt line with MENGE quantity, BWART movement type to filter for receipts (101), and BUDAT posting date for monthly aggregation. |
| MKPF | MBLNR | Material document headers — MKPF provides document-level context and BUDAT posting date for temporal grouping, joined to MSEG via MBLNR. |
| MSEG | MBLNR | Material Document Number linking to goods receipt header |
| MKPF | BUDAT | Posting date for material document |

### Transformation (plain language)

1. This column carries the raw quantity values from SAP field MSEG.MENGE, flowing through unchanged from the material document line items table.
2. This column carries the material document number from SAP field MKPF.MBLNR, flowing through unchanged to enable goods receipt tracking and aggregation by month.
3. Counts the total number of goods receipt line items recorded in the period to give the monthly receipt volume.
   - *Join:* Join MSEG to MKPF on MBLNR for document date aggregation
   - *Filter:* Filter BWART = '101' for goods receipts into warehouse stock
4. Truncates the goods receipt posting date to the first day of its calendar month, grouping all receipts within the same month together for monthly aggregation.
   - *Join:* Header table for document date information
   - *Filter:* All posting dates

### SQL (from dbt models)

**fact_goods_receipts_monthly.receipt_line_count:**
```sql
COUNT(*)
```

**fact_goods_receipts_monthly.receipt_month:**
```sql
DATE_TRUNC('month', grh.posting_date)
```

### Target Models

- `fact_goods_receipts_monthly`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

Status: `ready_for_s2t`

## Related Decisions (4)

- **#91** (2026-04-29) — lock_demo_happy_path_term_candidate_b_goods_receipts: Candidate B locked as demo's happy-path contrast term. Walk happens in fresh session. Investigation context preserved in an uncommitted session note. Backups identified. This KD captures both the decision and the architectural reasoning (gate-evaluation unity over binary works/refuses framing) so the rationale survives the session boundary.
- **#92** (2026-04-29) — ki112_ki113_empirically_validated_stage_c_self_corrects: KI-113's negative-feedback loop is the load-bearing fix. KI-112's directive prevents the most-frequent failure mode (column-table mismatch) but the LLM can still hit unrelated typing errors; KI-113 surfaces those errors so the LLM corrects within 1-2 turns instead of looping until budget exhaustion. Stage C now produces ready_for_s2t terms reliably for the BG029 demo path. Stage E Create S2T button is available in the UI for BG029.
- **#95** (2026-05-05) — wire_dbt_compile_into_end_of_task_for_business_glossary_cache_freshness: End_of_task is the right wiring point: it already runs after every task per CLAUDE.md commit-gate rule, already gates dbt activity on model_changes/seed_changes, already runs `dbt seed`. Adding `dbt compile` here costs ~5-10s per gated run and eliminates the 'not in dbt cache' caption for normal usage. Stage E's narrow `dbt run --select` was a deliberate scope choice (don't rerun unrelated marts); cache freshness is end_of_task's job, not Stage E's.
- **#96** (2026-05-05) — rule3_enforcement_checker_plus_two_mart_refactors: RULE 3 now has a checker. Two of three violators refactored to vault-sourced patterns matching fact_purchase_orders / fact_invoices conventions. The third is allowlisted with a clear fix path (KI-121: build sat_invoice_item + zmm_approval_log staging+vault, then refactor). Future model changes that introduce new RULE 3 violations will hard-fail end_of_task.py.
