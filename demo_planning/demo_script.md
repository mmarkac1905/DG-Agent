# CPE Procurement Analytics — Demo Script

_Working guide for the 8.4.7 demo run. Not a pitch doc — written for the operator driving the demo._

## 1. Pre-demo setup checklist

Before walking the audience through any tab or CLI:

- [ ] DuckDB at `cpe_analytics.duckdb` loaded; `raw_sap.*` has **41 tables**, including the new `raw_sap.zmm_approval_log` (500 rows)
- [ ] Seeds synchronized: `dbt seed --full-refresh` green. `main_seeds.domain_analysis_results` has DARs for `zmm_approval_log` across all applicable analyzers
- [ ] `main_seeds.semantic_model` has at least **one row** (`zmm_approval_log`) — the first time Layer A has non-empty content in this project
- [ ] `main_seeds.dbt_semantic_model` has **93 rows** from Layer B compile (one per dbt model)
- [ ] `main_seeds.business_glossary` has **28 terms** including the new `BG028 po_invoice_three_way_match_variance`
- [ ] `main_seeds.business_term_analysis_results` has at least one BAR for BG028 (from the live Piece 8 run — may be `converged` or `hard_stop`; both demonstrate the architecture)
- [ ] Non-LLM regression `python scripts/regression_piece8.py` returns **13/13 pass**
- [ ] `dbt test` green across the BAR + Layer A + Layer B seeds

## 2. Architecture narrative (30-second opener)

> "The system builds canonical SQL-writing conventions automatically from observed data. Three consumer priorities: **ontology first** (dbt-modeled tables — Layer B reads dbt's manifest.json deterministically), **Layer A second** (raw tables without dbt coverage — the LLM synthesizes conventions from EDA DARs), **base LLM knowledge third** (fallback). The glossary defines what a business term means; the system generates the SQL to answer it, grounded in both layers."

Two talking points to land:

- The EDA framework (8 analyzers now) is **source-agnostic**. SAP today, any structured source tomorrow. Analyzers are pure SQL except four LLM-driven ones; none hand-authored per source.
- Layer A is the edge vs. dbt semantic layer / Cube / Wren AI: **compiled from observed data, never hand-authored**. New sources get conventions automatically from their first EDA pass.

## 3. Demo flow (CLI walkthrough — UI arrives in Piece 9)

### Step 1 — "Here are our source tables" (inventory, 30s)

Show `demo_planning/sap_inventory_report.md` top section. Call out:
- 41 raw SAP tables loaded; 36 staging-covered, 5 raw-only (Z-table + 4 legacy empty tables)
- **`zmm_approval_log`** is the new entrant — a custom Z-table for invoice-approval workflow. No staging. No mart. Pure raw.

### Step 2 — "We run 8 analyses automatically" (30s)

Open `scripts/run_*_analysis.py` listing. Name the 8:
1. Completeness (null rates)
2. Dimensions (cardinality)
3. Magnitude (row counts + top buckets)
4. Code Tables (code-to-description decoding)
5. Temporal Coverage (Phase 2)
6. Performance Baseline (Phase 2 — co-emitted with Magnitude)
7. Grain Relationship (Phase 2 — header/detail sum-match detection)
8. Segmentation Threshold (Phase 2)

### Step 3 — "Here's what we learned" (60s)

Show a DAR row for `zmm_approval_log`:
```sql
SELECT analysis_type, COUNT(*) FROM main_seeds.domain_analysis_results
WHERE source_tables LIKE '%zmm_approval_log%' GROUP BY analysis_type;
```
Highlight 2-3 findings. Good choices:
- `temporal_coverage`: APPR_DATE spans 771 days, 7% null (= the 35 pending records, as expected)
- `performance_baseline`: TOL_AMT avg=1056 EUR, p25=544, p75=1562 — the LLM sees typical values when writing aggregate SQL
- `segmentation_threshold`: TOL_AMT quartile thresholds [544, 1069, 1562]

### Step 4 — "Layer A — EDA-compiled conventions" (90s, the core moment)

Query Layer A for `zmm_approval_log`:
```sql
SELECT table_name, canonical_alias, entity_class, primary_key_cols,
       typical_join_keys_json, code_column_refs_json, reference_sql
FROM main_seeds.semantic_model WHERE table_name='zmm_approval_log';
```

Point out:
- `canonical_alias='zal'` — the LLM picked a 3-char SAP-convention alias the way a senior engineer would
- `entity_class='fact'` — workflow events are fact-grained
- `typical_join_keys_json` has the correct PO / PO-line joins
- `reference_sql` — a working 3-line query exemplar using `raw_sap.zmm_approval_log` (no `ref()` because no dbt model exists for this table yet)
- Phase 2 fields populated: temporal_coverage / typical_values_range / natural_thresholds all carry real numbers sourced from the DARs

Key narrative: **"If we add another custom SAP Z-table tomorrow, its Layer A row will compile automatically on the next EDA cycle. No code per source."**

### Step 5 — "Layer B — dbt metadata as conventions" (60s)

Query Layer B for a staging model:
```sql
SELECT model_name, dbt_layer, materialized, canonical_alias, primary_key_cols,
       exposed_columns_json, reference_sql
FROM main_seeds.dbt_semantic_model WHERE model_name='stg_sap__ekko';
```

Point out:
- Deterministic extraction from `dbt/target/manifest.json` — no LLM
- `reference_sql` uses `{{ ref('stg_sap__ekko') }}` canonical Jinja form — the seed is the single source of truth
- Assembler rewrites Jinja to `main_staging.stg_sap__ekko` literal form for the iteration gate (mechanical gate runs raw DuckDB, doesn't render Jinja); preserves Jinja form for Create S2T (dbt compiles it)

### Step 6 — "A complex business term" (30s)

Show `BG028` glossary row:
```sql
SELECT id, term_name, display_name, definition, status
FROM main_seeds.business_glossary WHERE id='BG028';
```

Emphasize: 7 scope tables (ekko, ekpo, ekbe, rbkp, rseg, lfa1, zmm_approval_log). Three-way matching within 5% tolerance. This is materially harder than single-table aggregations.

### Step 7 — "Pre-S2T Reasoning on BG028" (2 min, narration-heavy)

Run:
```
python scripts/run_term_injection.py --term-id BG028 --max-iters 8 --budget-cap 2.00
```

What to narrate while the iteration loop runs:
- "The runner assembled a ~20K-token context bundle. Static catalog, Layer A (zmm_approval_log), Layer B (6 covered tables), business definitions, DARs from the EDA framework."
- "4-breakpoint prompt caching is active — the bundle is cached across iterations; only the per-iteration task tail is uncached."
- "Every iteration passes through a mechanical gate (raw DuckDB compile+run) and an AST citation audit (every identifier in the SQL must trace to something in the bundle text)."

### Step 8 — "What the system produced" (2 min)

Query the BAR:
```sql
SELECT status, convergence_reason, confidence, iterations_count,
       llm_total_cost_usd,
       semantic_model_consumed, dbt_semantic_model_consumed,
       LENGTH(iteration_trace) AS trace_bytes
FROM main_seeds.business_term_analysis_results
WHERE business_term_id='BG028' ORDER BY executed_at_utc DESC LIMIT 1;
```

**If `converged`:** narrate the convergence path, show `final_query_sql`, walk through the Layer A + Layer B references in the SQL.

**If `hard_stop`:** narrate the failure honestly. _"The AST citation audit caught a regex false-positive on numeric literals — the architecture failed safe, didn't produce a wrong answer, and persisted the full trace for audit. This is how we'd diagnose it in production."_ Show the proposed SQL (which is actually good — correct three-way match logic), show which gate caught it, show what we'd fix next (audit regex refinement).

Either outcome demonstrates the three-layer consumption architecture. The LIVE 2026-04-21 run produced hard_stop on iter 1 at $0.29 — both Layer A and Layer B attestation were populated correctly.

### Step 9 — (Future) "Promote + Create S2T"

Mark as **8.5 scope**, coming next. Describe the flow:
1. Analyst reviews the BAR, confirms the SQL matches intent, sets `status='promoted'` via Streamlit
2. Create S2T call consumes the promoted BAR + emits dbt model SQL using `{{ ref() }}` Jinja (Layer B reference_sql preserved unchanged)
3. dbt compiles and runs; the term becomes queryable in production

## 4. Possible failure modes to narrate if they happen

- **Piece 8 hard_stops on citation audit**: "Architecture failed safe, SQL caught before running. Show the trace, name the gate, show what the LLM attempted to cite."
- **Context assembler warning about DuckDB connection config**: Known_issue #31 — the assembler opens read-only while the runner holds read-write. Benign — the bundle still assembles, just with a warning.
- **Layer A empty for existing glossary scope**: Expected. All 23 previous terms target fully ontology-covered scopes, so Layer A is empty for them. BG028 is the first term that activates Layer A.
- **Code Tables analyzer auto-picks wrong column**: Known pattern (known_issue #27). Re-run with explicit `--code-column` to fix.

## 5. Re-run instructions (reproducibility)

Full reset + rebuild:

```
# 1. Regenerate the Z-table and its 500 sample rows
python scripts/generate_zmm_approval_log.py

# 2. Seeds
cd dbt && dbt seed --full-refresh && cd ..

# 3. EDA — Phase 2 analyzers first (deterministic, no LLM)
python scripts/run_date_analysis.py --tables zmm_approval_log
python scripts/run_segmentation_analysis.py --tables zmm_approval_log
python scripts/run_grain_relationship_analysis.py --tables zmm_approval_log,rbkp

# 4. Re-seed DARs into DuckDB (analyzers write CSV only)
cd dbt && dbt seed --full-refresh --select domain_analysis_results && cd ..

# 5. EDA — LLM analyzers (now with dynamic layer content)
python scripts/run_magnitude_analysis.py --table zmm_approval_log
python scripts/run_completeness_analysis.py --table zmm_approval_log
python scripts/run_dimensions_analysis.py --table zmm_approval_log
python scripts/run_code_tables_analysis.py --table zmm_approval_log --code-column APPR_STATUS
python scripts/run_code_tables_analysis.py --table zmm_approval_log --code-column REASON_CODE

# 6. Re-seed DARs
cd dbt && dbt seed --full-refresh --select domain_analysis_results && cd ..

# 7. Compile Layer A (LLM synthesis)
python scripts/compile_semantic_model.py --tables zmm_approval_log

# 8. Compile Layer B (deterministic)
python scripts/compile_dbt_semantic_model.py --force

# 9. Live Piece 8
python scripts/run_term_injection.py --term-id BG028 --max-iters 8 --budget-cap 2.00

# 10. Inspect BAR
python -c "import duckdb; c=duckdb.connect('cpe_analytics.duckdb', read_only=True); print(c.execute('SELECT status, convergence_reason, confidence, llm_total_cost_usd, semantic_model_consumed, dbt_semantic_model_consumed FROM main_seeds.business_term_analysis_results WHERE business_term_id=\\'BG028\\' ORDER BY executed_at_utc DESC LIMIT 1').fetchone())"
```

Budget: ~$0.30 EDA + ~$0.30 Layer A compile + ~$0.30-2.00 Piece 8 = **~$1.00-2.60 per full re-run**.

Cached re-runs (within 5 min of the first) hit the 4-breakpoint cache → ~50% cost reduction after iteration 1.

---

_End of demo script._
