# CLAUDE.md — agent & contributor guide

Operating notes for working in this repo (with an AI agent or by hand). For the
project overview, quickstart, and architecture see **[README.md](README.md)**;
this file covers the conventions that keep changes consistent.

## What this project is

An AI-native data-governance layer over SAP MM data: a business term goes from a
one-line definition to a deployed, grain-validated dbt mart through an LLM
pipeline with deterministic guardrails. Domain (example, synthetic): CPE
(Customer Premises Equipment) procure-to-deploy analytics for a fictional
operator, **Helios Telecom**. Stack: DuckDB · dbt · Streamlit · Claude.

## Data architecture (layers)

```
Layer 0  RAW        SAP tables loaded into DuckDB (synthetic sample data)
Layer 1  STAGING    dbt views — clean / rename / type-cast SAP fields to business names
Layer 2  VAULT      dbt incremental — Data Vault 2.0 (hubs, links, satellites)
Layer 3  MARTS      dbt tables — Kimball star schema (dims, facts)
Layer 4  OBT        dbt views — flattened wide tables for BI
Layer 5  KNOWLEDGE  dbt views — computed business facts, KPIs, health checks
Layer 6  STREAMLIT  app/ pages reading from OBT + knowledge
```

**The knowledge graph lives in `dbt/seeds/`** — both the governance content
(`business_glossary`, `s2t_mapping`, `sap_data_dictionary`, `data_vault_design`,
`org_structure`, `procurement_rules`, `abap_logic_catalog`, `z_tables_catalog`,
`known_decisions`, `known_issues`) and the AI pipeline's run-state
(`domain_analysis_results`, `term_analysis_results`, `analysis_findings`,
`dbt_*` catalogs). The wiki under `knowledge/` is generated from these seeds by
`scripts/build_knowledge_wiki.py`. Source of truth is the seeds + DuckDB, not
the markdown.

## The governance layer (what replaces Collibra / Informatica)

| Capability | Traditional tool | Here |
|---|---|---|
| Business glossary | Collibra / Informatica | `business_glossary.csv` → `knowledge/business_glossary/` |
| Data catalog | Informatica scanner | `sap_data_dictionary.csv` + `schema.yml` → `knowledge/sap_tables/` |
| S2T mapping | Excel / Informatica | `s2t_mapping.csv` (LLM-generated + verified) |
| Lineage | Informatica / dbt docs | dbt DAG + wiki |
| Profiling | Informatica DQ | live DuckDB queries (`scripts/`) |
| Business rules | Collibra / manual | transformation logic in wiki (plain + SQL) |
| Approval workflow | Collibra | `status` field (`draft → scope_confirmed → approved`) |

Everything is code + seeds in git, queryable by an LLM — nothing locked in a
vendor SaaS.

## SAP domain context (Procure-to-Deploy, MM module)

| Step | Txn | Key tables | What happens |
|---|---|---|---|
| Purchase requisition | ME51N | EBAN, EBKN | internal request for CPE |
| Purchase order | ME21N | EKKO, EKPO, EKET | order sent to vendor |
| Goods receipt | MIGO (101) | MKPF, MSEG | CPE arrives, stock up |
| Invoice verification | MIRO | RBKP, RSEG | vendor invoice matched to PO + GR |
| Stock management | — | MARD, MCHB | inventory per plant/location |
| Serial tracking | — | EQUI, EQBS | devices tracked by serial number |
| Deployment | MIGO (201) | MSEG | CPE issued for customer install |
| Customer return | MIGO (161) | MSEG | CPE returned |
| Vendor return | MIGO (122) | MSEG | defective CPE returned to vendor |

**ABAP/Z-tables** (`abap_logic_catalog`, `z_tables_catalog` seeds) document
custom SAP code — illustrative entries for typical telco customizations. When a
metric looks wrong, check whether custom ABAP logic modifies the data before it
reaches the standard table.

## Repository layout

```
app/        Streamlit UI + claude_api.py (the LLM pipeline: scope, EDA, S2T generation)
dbt/        models (staging/vault/marts/obt/knowledge), seeds (knowledge graph), tests
scripts/    data generators, EDA analyzers, the S2T pipeline runners, wiki builder
  prompts/  LLM prompt templates — one per pipeline stage/analyzer (all loaded at runtime)
knowledge/  generated governance wiki (regenerate with build_knowledge_wiki.py)
tests/      pytest suite + fixtures
```

## Vocabulary

S2T = source-to-target mapping. DAR / TAR / BAR = domain / term / business-term
analysis results — EDA findings at table, term, and full-analysis-run grain
(each is a seed of the same name). Layer A / Layer B = per-table SQL-writing
conventions (LLM-synthesized from EDA for raw-only tables / compiled from dbt's
manifest for dbt-covered ones). Stage 0–E = spec → scope → blockers → domain
EDA → term EDA (C′) → generation → deploy. F.3 = the post-generation join
validator that rejects `catastrophic_fanout` joins. See the README's
vocabulary table for the longer version.

## Source selection (multi-source runs)

The pipeline profiles ONE source schema per run, selected by env var:
`DG_SOURCE_SCHEMA` (default `raw_sap`) — read by `scripts/_source_config.py`
and used by every analyzer and stage. `DG_DOMAIN_CONTEXT` overrides the
business framing in generation prompts; `DG_AGENT_MODEL` overrides the model
(`scripts/_model_config.py`). The Olist second-source demo models live under
`dbt/models/olist/` behind `DG_ENABLE_OLIST=true` — the default build never
touches them. Onboarding recipe: README → "Pointing it at another source".
Tests always run against the default source (`tests/conftest.py` clears the
env vars).

## Conventions (read before changing things)

- **Run dbt from `dbt/`.** `dbt/profiles.yml` uses a relative DuckDB path
  (`../cpe_analytics.duckdb`); running dbt elsewhere writes to a stray DB.
- **RULE 3 — layering.** Mart / OBT / knowledge models must `ref()` **vault**
  models only, never staging or `raw_sap`. This is enforced at generation (the
  `create_s2t` prompt) and at commit (`scripts/check_rule3_layer_violations.py`).
- **Verify before claiming a data gap.** Query the actual tables; don't assume
  from memory. The knowledge graph (`known_decisions`, `known_issues`) records
  what was built and why.
- **After a model change:** `dbt seed && dbt run && dbt test`. After a seed
  change that affects the wiki: `python scripts/build_knowledge_wiki.py`.
- **Record findings** (analysis results, design decisions, bugs, resolved
  issues) into `known_decisions.csv` / `known_issues.csv` — the project's
  reasoning is itself version-controlled and queryable.

See `knowledge/anti_patterns.md` for the hard-won lessons (things already tried
and rejected) before proposing a change.
