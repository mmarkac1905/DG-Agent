# Business Term: Average Vendor Lead Time

_Last generated: 2026-07-04 02:20:08_

## Definition

Average calendar days between purchase order creation and goods receipt posting, per vendor per month

- **ID:** `BG001`
- **Owner:** Procurement Department
- **Approved by:** Head of Supply Chain
- **Status:** `approved`
- **Unit:** days
- **Grain:** vendor × month
- **Domain:** procurement
- **Related terms:** [on_time_delivery_rate](on_time_delivery_rate.md) · [vendor_scorecard](vendor_scorecard.md)

**Notes:** Excludes GR reversals (mvt 102). Nulls = open POs without GR yet.

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| EKKO | BEDAT | PO creation date |
| EKKO | LIFNR | Vendor account number |
| MKPF | BUDAT | GR posting date |
| MSEG | EBELN | PO reference on GR |

### Transformation (plain language)

1. This column is a straight pass-through of the document date from the purchase order header, converted to a standard date format.
   - *Join:* Header table — one row per PO
   - *Filter:* All PO types (BSART)
2. This column carries the vendor account number directly from SAP field EKKO.LIFNR without any modification through the data pipeline.
   - *Join:* Header table — join to hub_vendor
   - *Filter:* All vendors
3. The column captures the earliest goods receipt date from all goods receipt documents for a purchase order, converted from SAP's internal date format.
   - *Join:* Header of material document
   - *Filter:* Only GR documents
4. This column calculates the number of days between the purchase order date and the first goods receipt date for each purchase order, representing the actual vendor lead time.
   - *Join:* MSEG.EBELN = EKKO.EBELN
   - *Filter:* Movement type 101 only — exclude reversals (102) and returns (122)

### SQL (from dbt models)

**fact_purchase_orders.po_date:**
```sql
CAST(BEDAT AS DATE)
```

**fact_purchase_orders.first_gr_date:**
```sql
MIN(grh.posting_date)
```

**fact_purchase_orders.lead_time_days:**
```sql
CASE WHEN fgr.first_gr_date IS NOT NULL AND ph.po_date IS NOT NULL
        THEN CAST(fgr.first_gr_date - ph.po_date AS INTEGER)
        ELSE NULL
    END
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

APPROVED — Business owner approved definition (2026-04-13)

## Related Decisions (8)

- **#9** (2026-04-14) — marts_and_obt_built: Full analytical stack complete: raw -> staging -> vault -> marts -> OBT. Fixed three spec bugs: hk_po_item vs hk_po_material mismatch gr_totals hk_material_document join fact_invoices hk_vendor not in sat.
- **#16** (2026-04-14) — business_glossary_audience_split: Two-audience split is the default pattern for any governance UI in this project. Never collapse back into a single tab.
- **#58** (2026-04-18) **[NEVER_REPEAT]** — empty_csv_corrupts_duckdb_schema_inference: After any wipe event that resulted in a --full-refresh against a header-only CSV, DO NOT try to re-populate via non-refresh dbt seed. Always run dbt seed --full-refresh --select <seed> once against the restored CSV before resuming normal operations. Consider adding a guard in end_of_task.py CRLF-normaliser loop: if a seed CSV has only a header line (no data rows), skip the subsequent --full-refresh for that seed so the prior valid schema stays in DuckDB. See also decision #57 csv_dictwriter_truncate_trap — this is the downstream collateral symptom.
- **#75** (2026-04-19) — semantic_gate_catching_missing_filter_is_success_not_failure: Gate-catches-LLM-bug flow is the system working as designed per decision #63. Do not chase prompt perfection when the gate is the correct enforcement surface.
- **#76** (2026-04-19) — phase_15a_closed_on_p74_with_honest_acknowledgments: Migration complete. assemble_context() helper + per-source consumption directives are the standing pattern for all LLM calls going forward. Create S2T migration validated. Next work (term-analysis UI, chat) layers on this foundation. Future LLM-call-containing features should default to the directive-per-source-type template instead of rediscovering 'bundle delivery alone is insufficient' (decision #73).
- **#77** (2026-04-19) — ab_test_p75_accepts_partial_quality_evidence_token_efficiency_core_claim: A/B results accepted. Known issues #28 and #29 logged. The term-analysis runner design must include an ontology-layer consumption directive. Migration closure stands — retry loop + gate infrastructure are the validation layer for LLM output quality in production, not regression tests. The A/B results JSON log preserved; token-efficiency comparison table is the empirical evidence. IMPORTANT: this decision does NOT claim NEW path produces higher-quality SQL than OLD path. A/B quality evidence is single-run and partial (4 of 6 quality signals lost to harness bug #28). The defensible claims are: (a) NEW path is 54-79% more token-efficient; (b) NEW path has zero invented citation IDs across 3 runs; (c) NEW path architectural properties (fingerprinting, explicit directives, audit) enable the term-analysis injection loop which OLD path could not support. Quality parity between paths for single-shot Create S2T remains an assumption validated only in production by the deploy auto-retry + semantic gate. If a production regression emerges on NEW path that OLD path would not have shown, re-open this decision.
- **#105** (2026-05-05) — phase_beta_slim_vendor_material_z_tables_to_genuinely_ht_fields_only: This closes the SAP-native-default cleanup cycle the user requested with 'why dont we keep with true SAP native fields mostly'. Z-tables now carry ONLY fields with no SAP home: vendor (equipment_types - HT CPE taxonomy, quality_rating - HT internal scoring, contract_status - HT lifecycle), material (lifecycle_months - HT warranty/TCO horizon, primary_vendor_id - HT preferred supplier; SAP EORD can list many sources without designating a primary). Lesson generalized in feedback_default_sap_native memory: default assumption is real SAP carries the data; invent only for genuinely HT-domain content. Remaining seeds (org_structure, abap_logic_catalog, z_tables_catalog) still need their own architectural decisions - out of scope for this pass.
- **#107** (2026-05-12) **[NEVER_REPEAT]** — order_size_does_not_affect_lead_time_days: No meaningful effect — vendor capacity is not the bottleneck at these volumes. Splitting large orders would not gain lead-time reduction. Migrated from decommissioned signal_relationships #4 (2026-05-12, decision #106). Re-test only if HT volume mix changes significantly (e.g., 10x scale-up).

## Open Issues (2)

- **#29** [open/medium] Create S2T directives cover 4 dynamic sources but not ontology layer — LLM can pick production-model names and create duplicates — The Create S2T A/B test showed the new path on BG001 generated a model named fact_purchase_orders, which already exists in dbt/models/marts/fact_purchase_orders.sql. The bundle ontology layer contained the existing_models list — LLM had access to it — but picked a colliding name.…
- **#94** [open/low] Scope-aware ordering for the term-analysis DAR loader (Option beta) - deferred follow-up to #93 — During #93 fix design, scope-aware ORDER BY was investigated as Option beta: replace LIMIT 50 ORDER BY executed_at_utc DESC with a scope-overlap-first prioritization (rows whose source_tables overlap the term scope rank above non-overlapping rows; recency within priority). Premis…
