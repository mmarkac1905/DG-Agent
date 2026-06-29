# CPE Procurement Analytics — Claude Code Instructions

## Session Startup — READ THESE FIRST

Before ANY work in this project, read these files in order:
1. This file (CLAUDE.md) — project overview and architecture
2. knowledge/anti_patterns.md — HARD RULES and lessons learned (13 rules). NEVER violate these.
3. knowledge/dv_verification_report.md — DV 2.0 audit results
4. dbt/seeds/known_issues.csv — open issues to be aware of
5. dbt/seeds/known_decisions.csv — architectural decisions already made

Then run this verification query to confirm the database is healthy:

```bash
python -c "
import duckdb
conn = duckdb.connect('cpe_analytics.duckdb', read_only=True)
schemas = conn.execute('SELECT table_schema, COUNT(*) FROM information_schema.tables GROUP BY table_schema ORDER BY 1').fetchall()
print('=== DATABASE STATE ===')
for s, c in schemas: print(f'  {s}: {c} tables')
issues = conn.execute(\"SELECT id, title, status FROM main_seeds.known_issues WHERE status='open'\").fetchall()
if issues:
    print(f'\n=== OPEN ISSUES ({len(issues)}) ===')
    for i in issues: print(f'  {i[0]}: {i[1]} [{i[2]}]')
else:
    print('\nNo open issues.')
conn.close()
"
```

## Current State (2026-05-05)
- Staging: 36 views | Vault: 34 incremental | Marts: 14 | OBT: 5 | Knowledge: 5
- Streamlit: 9 pages (5 dashboard + 4 governance)
- Pipeline: scan_dbt → sync_s2t_sql → sync_s2t_plain (LLM) → export_parquet (all in end_of_task.py)
- DV audit: 31/33 pass, link_po_item added
- **Phase 15a CLOSED** (decision #76, token-efficiency evidence via P7.5 decision #77). Helper + per-source consumption directives + bundle fingerprinting + audit live in production. `create_s2t_with_implementation` is the production path; OLD pre-P7.2 fallback removed 2026-05-04.
- **Phase 15b Piece 8 LANDED through 8.5.2** — Pre-S2T Reasoning Layer (BAR producer) → Create S2T BAR-consumer → 8.5.1 stabilization → 8.5.2 sap_data_dictionary backfill (326/326 cells, 42/42 tables). Canonical design at `context/phase_15b_piece_8_pre_s2t_reasoning_layer.md` (v3.10).
- **Piece 9 Stage A (LLM-driven scope derivation) LANDED** — exercised end-to-end via the BG029 demo walk (Stage A→E, demo checkpoint `2ce7bc0`, 2026-04-29).
- OBT layer column-level schema docs (113/113) added 2026-05-05; vault/staging/marts column docs deferred as internal layers.

## Pending
- LinkedIn image from Gemini
- **Open known_issues:** #24 (hashdiff NULL vs '' collapse), #25 (raw_sap.* ingestion_date missing — latent, blocks age-based staleness UI per decision #72), #29 (Create S2T ontology consumption directive missing — LLM can pick production-model names). Marts/knowledge layer column-level schema docs deferred to production rollout.

## Project Overview
Data Product MVP for Helios Telecom. Domain: CPE (Customer Premises Equipment) procurement and lifecycle analytics. Source system: SAP MM module. Stack: DuckDB + dbt + Streamlit.

Business context: HT manages thousands of CPE devices (routers, ONTs, set-top boxes, modems, switches) across their network. This project builds analytics for the full procure-to-deploy lifecycle: purchase requisition → purchase order → goods receipt → inventory → deployment → return/defect tracking.

## Session Startup
1. Run `scripts/session_context.sql` against cpe_analytics.duckdb to get full project context — **MANDATORY on first turn of every session**. Execute via `python -c "import duckdb; conn = duckdb.connect('cpe_analytics.duckdb'); [print(row) for row in conn.execute(open('scripts/session_context.sql').read()).fetchall()]"` (duckdb CLI is not on PATH on Windows).
2. This gives you: system overview, seed health, session agenda (NEXT_SESSION items, overdue reminders, open issues), never-repeat decisions, data product status
3. NEVER say "I don't have context" — the database has everything
4. Knowledge graph lives in dbt seeds: `known_decisions`, `known_issues`, `sap_data_dictionary`, `business_glossary`, `s2t_mapping`, `procurement_rules`, `org_structure`, `data_contracts`, `data_vault_design`, `abap_logic_catalog`, `z_tables_catalog`. Entity enrichment (vendor / material / movement_type) lives in vault sats, not seeds.
5. Context files in `context/` are auto-generated exports — regenerate with `python scripts/export_context.py`

## Session Start Behavior

After running session_context.sql, check the SESSION_AGENDA section. Present items by priority:

1. **NEXT_SESSION** items: explicit work the prior session set up. Always mention first.
2. **DUE_REMINDERs**: overdue reminders. Ask: "This was due on [date]. Want me to work on this?"
3. **OPEN_ISSUEs**: by priority. Present as brief summary.
4. **If agenda is empty**: "System healthy. No pending actions. What would you like to work on?"

Present as a brief summary first, then ask what to tackle. Don't dump everything at once.

## Data Architecture Layers

```
Layer 0: RAW        — SAP tables loaded into DuckDB (sample data, ~50K records per major table)
Layer 1: STAGING    — dbt views: clean, rename, type-cast SAP fields to business names
Layer 2: VAULT      — dbt incremental: Data Vault 2.0 (hubs, links, satellites)
Layer 3: MARTS      — dbt tables: Kimball star schema (dims, facts)
Layer 4: OBT        — dbt views: flattened wide tables for Streamlit
Layer 5: KNOWLEDGE  — dbt views: computed business facts, KPIs, health checks
Layer 6: STREAMLIT  — dashboard pages reading from OBT + knowledge views
```

## Data Governance Layer (Business Glossary + Data Catalog)

This project includes a self-service data governance layer that replaces traditional tools like Informatica/Collibra:

| Capability | Traditional Tool | Our Implementation |
|---|---|---|
| Business Glossary | Collibra / Informatica | `dbt/seeds/business_glossary.csv` → `knowledge/business_glossary/` wiki pages |
| Data Catalog | Informatica Metadata Scanner | `dbt/seeds/sap_data_dictionary.csv` + `schema.yml` → `knowledge/sap_tables/` wiki pages |
| S2T Mapping | Excel / Informatica Mapping | `dbt/seeds/s2t_mapping.csv` → wiki pages show source→target for each business term |
| Data Lineage | Informatica / dbt docs | dbt DAG + wiki pages show vault lineage path |
| Data Profiling | Informatica Data Quality | `scripts/profile_data.py` computes live stats from DuckDB |
| Business Rules | Collibra / manual docs | Transformation logic in wiki (plain language + SQL side by side) |
| Approval Workflow | Collibra workflow | `status` field in business_glossary.csv (draft → approved) |

**The key difference:** All of this is generated from code and seeds, version-controlled in git, and queryable by AI. Nothing is locked in a vendor's SaaS platform.

**Workflow for adding a new business term:**
1. Business owner defines term in `business_glossary.csv` (or Streamlit form)
2. Data engineer adds S2T mapping in `s2t_mapping.csv`
3. Data engineer builds dbt model implementing the transformation
4. Run `python scripts/build_knowledge_wiki.py` to regenerate wiki
5. Business owner reads `knowledge/business_glossary/{term}.md` — sees definition, source tables, transformation logic (plain language + SQL), data profile, lineage
6. Business owner approves or requests changes
7. Status updated in seed, wiki regenerates

## ABAP Custom Code Layer

The `abap_logic_catalog` and `z_tables_catalog` seeds document custom ABAP code in HT's SAP system. This is knowledge that traditionally lives only in ABAP developers' heads.

**In a real engagement**, this is populated by:
1. Export ABAP source code (TADIR + REPOSRC tables) → give to Claude
2. Claude reads the code → extracts business rules, table dependencies, risk levels
3. Output written to seeds → wiki auto-generates documentation

**For the MVP**, we pre-populated realistic entries based on typical telco SAP customizations.

**Why this matters:**
- When building dbt models, check if a Z-table is involved in the data flow
- When a business metric looks wrong, check if custom ABAP logic modifies the data before it reaches the standard table
- When adding a new movement type or material, check which ABAP programs need updating
- The `abap/overview.md` wiki page shows the complete table dependency graph — which programs read and write which tables

**Key custom programs to be aware of:**
- `ZMM_AUTO_EQUI_CREATE` — automatically creates equipment records at GR (critical for CPE tracking)
- `ZSD_CPE_PROVISIONING_TRIGGER` — bridges SAP to OSS for service activation (critical for customer experience)
- `ZMM_VENDOR_EVAL_SCORE` — calculates vendor scores that feed the vendor_scorecard data product
- `ZMM_CPE_WARRANTY_TRACK` — records warranty periods that feed TCO calculations

## SAP Domain Context

This project covers the **Procure-to-Deploy** process in SAP MM for a telco:

| Process Step | SAP Transaction | Key Tables | What Happens |
|---|---|---|---|
| Purchase Requisition | ME51N | EBAN, EBKN | Internal request for CPE equipment |
| Purchase Order | ME21N | EKKO, EKPO, EKET | Formal order sent to vendor |
| Goods Receipt | MIGO (mvt 101) | MKPF, MSEG | CPE arrives at warehouse, stock increases |
| Invoice Verification | MIRO | RBKP, RSEG | Vendor invoice matched to PO and GR |
| Stock Management | — | MARD, MCHB | Inventory tracked per plant/storage location |
| Serial Tracking | — | EQUI, EQBS | Individual CPE devices tracked by serial number |
| Deployment | MIGO (mvt 201) | MSEG | CPE issued to technician for customer installation |
| Customer Return | MIGO (mvt 161) | MSEG | CPE returned, stock increases, status updated |
| Vendor Return | MIGO (mvt 122) | MSEG | Defective CPE returned to vendor |

**Provisioning** (not in SAP): After physical CPE deployment, the network team activates the service — assigns IP, configures port on DSLAM/OLT, enables IPTV channels, sets bandwidth. This happens in OSS systems, not SAP. The SAP order triggers provisioning but provisioning data is outside this MVP scope.

## Before ANY Building, Change, or Suggestion (MANDATORY)

The knowledge wiki at `knowledge/` is the source of truth, generated from `dbt/seeds/` by `scripts/build_knowledge_wiki.py`. Read the wiki BEFORE writing code, not after.

1. **Read `knowledge/index.md`** for current system state
2. **Read the relevant page** — `data_products/`, `sap_tables/`, `domain/`, `data_vault/`
3. **Read `knowledge/anti_patterns.md`** to check if your idea was already tried and failed
4. ONLY THEN proceed with work

The wiki rebuilds from seeds. To force a rebuild after manual seed edits: `python scripts/build_knowledge_wiki.py`.

If you skip these reads and build something that contradicts the knowledge wiki, the work is INVALID and must be reverted.

## Before ANY Model Change (additional steps)
1. Record current baseline metrics before making changes
2. After changes, verify baseline metrics haven't regressed
3. Run `dbt seed && dbt run && dbt test` to verify

## Before ANY Dashboard Change
1. Note current displayed values
2. Make your change
3. Verify ALL previous values still display correctly
4. Never ship a dashboard change without testing all pages
5. **Do NOT redesign chart visuals unless explicitly asked**

## KNOWLEDGE GRAPH — MANDATORY RECORDING RULE

After completing ANY of the following, you MUST record findings to the knowledge graph before the task is considered complete — no exceptions:
- **Analysis** (any result including negative)
- **Model design decision** (Data Vault, mart, or staging)
- **Data quality finding**
- **SAP table relationship discovery**
- **Version change** (new dbt logic, schema changes)
- **Bug fix** → add to `dbt/seeds/known_decisions.csv`
- **Resolved issue** → update `dbt/seeds/known_issues.csv`

A fix or finding that isn't recorded in the knowledge graph didn't happen.

**After EVERY task run: `python scripts/end_of_task.py` — this is the commit gate. No exceptions.**

## CRITICAL: Always Verify Data Before Claiming Gaps

NEVER say "we don't have data for X" without first querying the actual database tables. Check the knowledge graph (`known_decisions`, `known_issues`) for context on what was built and when.

Do NOT assume data availability from memory — query it.

## Database Architecture
- **Knowledge graph:** 10 seeds + knowledge models (computed live from data)
- **Session startup:** run `scripts/session_context.sql` for complete context
- **Context export:** run `python scripts/export_context.py` to generate markdown for chat sessions
- **Source of truth is ALWAYS DuckDB, never markdown files**
- **Ontology:** dbt schema.yml files define what things mean

## Project Structure
```
dbt/           — models, seeds (knowledge graph), tests, macros
scripts/       — session_context.sql, build_knowledge_wiki.py, end_of_task.py
knowledge/     — auto-generated wiki from dbt/seeds/ (do not hand-edit)
app/           — Streamlit dashboard
context/       — auto-generated state markdown (via scripts/export_context.py)
validation/    — analysis results archive
```

## After EVERY Session (MANDATORY before ending)

Before ending the session, check:

1. **Did we discover anything new?** → append a row to `dbt/seeds/known_decisions.csv`
2. **Did we find a bug or issue?** → append a row to `dbt/seeds/known_issues.csv`
3. **Did we set up explicit next-session work?** → tag the relevant `known_issues` title with `NEXT_SESSION:` prefix
4. Run `python scripts/build_knowledge_wiki.py`
5. Review wiki output for any issues

This closes the loop: seeds → wiki → next session start. A finding that isn't in the seeds didn't happen.
