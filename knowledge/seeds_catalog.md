# `main_seeds` Schema — Table-by-Table Catalog

What's in DuckDB's `main_seeds` schema, what populates each table, and what reads from it. Source of truth for every row is the matching `dbt/seeds/<name>.csv` (per RULE 9, `dbt seed` syncs CSV → DuckDB; per RULE 1 + KI-117, the CSV is authoritative and DuckDB is the consumer view).

---

## A. Reference / master data — hand-maintained

Static lookup tables. Edited by hand (or by a one-shot scrape), committed to git, no continuous writer process. **Note:** as of 2026-05-05, vendor business enrichment moved out of this section into the **vault** — see `sat_vendor_business` (parented to `hub_vendor`). Other catalogs that should join to existing hubs are tracked as "production-rollout follow-ups" below.

| Table | Purpose | Notes |
|---|---|---|
<!-- cpe_catalog migrated 2026-05-05 to main_vault.sat_material_business — see decommissioned list below -->
| `movement_type_mapping` | BWART code → English description (`101=GR for PO`, `201=goods issue`, etc.) | Hand-maintained dictionary; consumed by `dim_movement_type`. **Correctly a seed** — pure reference dictionary, not entity attributes. |
| `org_structure` | HT corporate structure (purchasing orgs, plants, cost centers) | Hand-maintained. Hierarchical data; cleanest mapping is a new `hub_org_unit`. **Follow-up**. |
| `procurement_rules` | Business rules (DQ thresholds, approval limits) | Hand-maintained. Cross-cutting policy, not entity attributes — stays as a seed. |
| `data_contracts` | Source-system contracts (expected schemas, SLAs) | Hand-maintained; drives the Data Contract Compliance panel on the Business Glossary page (`app/pages/Business_Glossary.py:render_contract_compliance`). |
| `data_vault_design` | DV 2.0 entity catalog (which hub/link/sat exists, why) | Auto-synced from actual vault models by `scripts/scan_dbt_models.py:sync_vault_design_seed`; consumed by Business Glossary to compute vault-entity lineage per term. |
| `zmm_approval_status` | Z-table reference: APPR_STATUS code dictionary (`02=approved`, etc.) | Hand-maintained reference dictionary |
| `zmm_reason_codes` | Z-table reference: REASON_CODE dictionary | Hand-maintained reference dictionary |
| `abap_logic_catalog` | Custom ABAP programs documented for analyst reference | Hand-maintained. Considered for a future `hub_abap_program` if join volume warrants. |
| `z_tables_catalog` | Custom Z-tables documented (Z*-prefixed) | Hand-maintained |

**Decommissioned (2026-05-05)**:
- `vendor_catalog` — moved to `main_vault.sat_vendor_business` parented to `hub_vendor`, re-keyed to actual LIFNRs (Phase 1).
- `cpe_catalog` — moved to `main_vault.sat_material_business` parented to `hub_material`. MATNRs were already aligned; `primary_vendor` re-keyed from synthetic V001-V008 to actual LIFNRs so it joins to `hub_vendor` (Phase 2). `equipment_category` not carried in the sat — already derived in `dim_material` via CASE WHEN on `material_group`.
- `movement_type_mapping` — replaced by SAP-native vault (T156 + T156T → hub_movement_type + sat_movement_type + sat_movement_type_text). HT-domain `process_step` classification expressed as CASE WHEN in `dim_movement_type`. See decision #104.

**Decommissioned (2026-05-12)** — pre-public-release cleanup of decorative-only seeds:
- `strategy_configs` — trading-algo-schema artifact (columns: `hold_days`, `stop_pct`, `target_method`, `broker`, `backtest_wr`...). Only consumer was `scripts/build_knowledge_wiki.py` rendering it to a wiki page; no dbt model, app page, or LLM context loader read it.
- `signal_relationships` — hypothesis log (signal X → outcome Y, validated/invalidated). Only consumer was the wiki renderer; no analysis script consulted it to filter or score. CLAUDE.md "After EVERY Session" rule for it was removed in the same commit.
- `data_profile_config` + `scripts/profile_data.py` — earlier-generation EDA tool that wrote `knowledge/_profile_stats.json`. The JSON had no readers; `profile_data.py` was not in the `end_of_task.py` pipeline; the seed was loaded into a `profile_config` variable in `Business_Glossary.py` but the variable was never referenced downstream. The Phase 15b DAR/TAR/BAR pipeline (`run_*_analysis.py` × 10 + `_tar_writer.py` + `_bar_writer.py`) is the actual EDA framework and does not consult this seed.

---

## B. Knowledge graph — hand-maintained, gated by `end_of_task.py`

| Table | Purpose | Writer |
|---|---|---|
| `known_decisions` | Append-only architectural decision log. Every model change must add a row (CLAUDE.md commit-gate rule). | Hand-edited; `scripts/end_of_task.py` validates the gate (blocks commit if model changes without a new row). |
| `known_issues` | Open / resolved / wontfix issue tracker. NEXT_SESSION-tagged items are the routing surface. | Hand-edited / scripted append from chat sessions |

---

## C. Auto-generated — dbt scanner pipeline (in `end_of_task.py`)

These tables are **rebuilt every time models or seeds change**. Do NOT hand-edit — they get overwritten.

| Table | Writer | What it holds |
|---|---|---|
| `dbt_model_catalog` | `scripts/scan_dbt_models.py` | One row per dbt model: name, layer, materialization, ref()s, columns. Consumed by Streamlit (Data Model ERD, S2T Specification). |
| `dbt_column_lineage` | `scripts/scan_dbt_models.py` (+ `scripts/sync_s2t_plain_from_dbt.py` for the LLM-generated `transformation_logic_plain_business` column) | One row per (model, column): source expression, transformation type (direct / case_when / aggregation / etc), origin table+column. Powers the column-level lineage trace on Business Glossary. |
| `dbt_model_relationships` | `scripts/extract_dbt_relationships.py` | Inferred fact↔dim joinability from shared vault ancestors (RULE 3 — relationships are LOGICAL, not in `ref()`). |

Pipeline order (in `end_of_task.py`): `scan_dbt_models` → `extract_dbt_relationships` → `sync_s2t_from_dbt` → `sync_s2t_plain_from_dbt` → `dbt seed --full-refresh` (loads above into DuckDB) → `dbt compile` → `export_parquet`.

---

## D. Auto-generated — semantic-model layer (Phase 15b Piece 8 §22.5/§23.5)

Two separate tables that confused you — here's the difference:

| Table | Writer | Layer | Source of truth | Contents |
|---|---|---|---|---|
| **`semantic_model`** | `scripts/compile_semantic_model.py` (LLM-driven, Phase 15b §22.5) | **Layer A** | Raw SAP source tables WITHOUT dbt ontology coverage | LLM-synthesized canonical conventions per raw table: canonical alias, primary key, typical filters, typical joins, common traps, reference SQL. Used by the context assembler when no `dbt_semantic_model` row exists. |
| **`dbt_semantic_model`** | `scripts/compile_dbt_semantic_model.py` (deterministic, Phase 15b §23.5) | **Layer B** | `dbt/target/manifest.json` | Per-dbt-model canonical info extracted from the manifest (no LLM): columns, types, tests, depends_on, materialization, description. |

**When to consult which**: the context assembler prefers `dbt_semantic_model` (Layer B is ground truth from dbt) and falls back to `semantic_model` (Layer A) for raw sources that aren't yet wrapped by a dbt model. Both have a `populated_by` column (`human_override` / `auto_generated`); human-override rows are preserved across recompiles.

| Table | Writer | What it holds |
|---|---|---|
| `sap_data_dictionary` | `scripts/classify_source_columns.py` (LLM batch classification) + `scripts/run_catalog_backfill.py` (mass backfill) | Per (table, column) classification: business meaning, role (key / measure / dimension), notes. Powers Stage A scope derivation. |
| `sap_table_catalog` | `scripts/scrape_sap_catalog.py` (one-shot, scraped from public SAP docs) + updates from `scripts/run_term_injection.py` | Per-table SAP metadata (purpose, category, typical use cases). |
| `source_column_roles` | `scripts/classify_source_columns.py` | LLM-assigned column roles (key / measure / dimension / metadata / etc). |
| `source_column_role_changes` | `scripts/classify_source_columns.py` (audit log) | Append-only diff every time `source_column_roles` is re-classified. |

---

## E. Auto-generated — analysis pipeline (Stage B/C/E)

Continuously written by the LLM-analysis pipeline. Each row is one piece of analysis output.

| Table | Writer | What it holds |
|---|---|---|
| `domain_analysis_results` (DAR) | `scripts/run_*_analysis.py` (one writer per analyzer: completeness, dimensions, magnitude, code_tables, date, segmentation, grain_relationship, performance_baseline, bridge_coverage, join_cardinality) | One row per DAR — the atom of Stage B analysis. ~1200 rows currently. Status enum: success / error / skipped. |
| `term_analysis_results` (TAR) | `scripts/_tar_writer.py` invoked by `scripts/run_term_eda.py` | Per-iteration query+result for Stage C term EDA. Rows are grouped by `tar_id` per term. |
| `business_term_analysis_results` (BAR) | `scripts/_bar_writer.py` invoked by `scripts/run_term_injection.py` | Stage C → Stage E bridge: one BAR per term × iteration with `declared_sufficient` flag, validated S2T fragment, confidence, iteration trace. The Create S2T button gates on the latest BAR. |
| `domain_facts` | `scripts/refresh_domain_facts.py` (LLM synthesis from DAR reservoir) | Per-domain summarized facts (e.g., "Croatia uses EUR after 2023-01-01") used to ground Stage B prompts. |
| `domain_reports` | `app/pages/Data_Analysis.py` (LLM-driven, on user trigger) | Per-domain markdown report (analyst-facing summary). |
| `analysis_findings` | `app/pages/Data_Analysis.py` | Per-term findings persisted from analyst review of DARs. Note: per RULE 42 / decision #67, findings stay with the archived `term_id` and do NOT inherit to re-created same-named terms. |

---

## F. Application state + audit logs

| Table | Writer | Purpose |
|---|---|---|
| `business_glossary` | `app/pages/Business_Glossary.py` (term creation), `app/archive_term.py` (archival), `scripts/_scope_derivation.py` (Stage A derived scope), `scripts/_stage_a_blocker_loader.py` (Stage A blockers) | Business term registry. Each term row has definition, grain, owner, status (draft / approved / archived), filter_description, scope_derivation_history_json. The Streamlit UI's primary state. |
| `s2t_mapping` | Mostly auto-generated by `scripts/sync_s2t_from_dbt.py` from dbt ref-trace; Stage E adds rows when a new term is deployed; some hand-edits allowed (per RULE 1 with caveats). `app/_csv_safeguard.py` enforces row-count guardrails. | Source-to-target column mappings. The deliverable for the data-product handoff. |
| `archive_log` | `app/archive_term.py` | Append-only audit of term archival events (who, when, why, what got moved). |
| `data_qa_log` | `app/pages/Data_Analysis.py` | Append-only audit of DQ-rule events from the analyst's Data Analysis tab. |
| `ingestion_log` | `scripts/generate_sap_sample_data.py` | Append-only ingestion event log. Drives the staleness-banner UI per decision #40. (Per KI-25, raw_sap.* tables don't yet carry `ingestion_date` for per-row staleness.) |

---

## How to navigate this

- **"Where does table X come from?"** Look up the table here, find the writer, read that script's docstring.
- **"Is this safe to hand-edit?"** Sections A, B, F are hand-editable (with guardrails). Sections C, D, E are auto-rebuilt — hand edits will be overwritten.
- **"Why are there so many seeds?"** Each one represents either (a) reference data the project needs that doesn't live anywhere else, or (b) the materialized output of a pipeline that the next pipeline stage consumes via dbt. The `dbt seed` step is the convention for getting Python-produced data into DuckDB so dbt models can `ref()` it.
- **"Production-readiness — entity enrichment goes in the vault, not in seeds."** As of 2026-05-05, the project's policy is: if a hand-maintained reference dataset is **about a business entity that already has a hub** (vendor, material, plant, equipment, PO, etc.), it belongs in a satellite parented to that hub — NOT a standalone seed. The seed is `dbt/seeds/<name>.csv`-style only when (a) the data is pure reference dictionary (BWART codes, status codes), (b) cross-cutting policy not attached to one entity (procurement rules), or (c) governance/state (known_decisions, known_issues, signal_relationships). The vendor cleanup (`vendor_catalog` → `sat_vendor_business`) is the worked example. Production rollout: cpe_catalog, org_structure should follow the same pattern.
- **"Why two `*_semantic_model` tables?"** See Section D. Layer A (LLM, raw tables) and Layer B (deterministic, dbt models). Layer B is preferred when the table is wrapped by dbt; Layer A is the fallback for un-wrapped raw sources.

Maintained by hand. If a table is renamed / added / removed, update this doc as part of the same commit. Last revised 2026-05-05.
