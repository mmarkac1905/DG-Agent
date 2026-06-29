# DG AI Agent — AI-native Data Governance for SAP

> A self-service **data governance layer** that replaces Collibra / Informatica with **code + AI**.
> Everything — glossary, catalog, lineage, source-to-target mappings, business rules — lives in
> version-controlled seeds and dbt models, and is **queryable and extensible by an LLM**.

**Stack:** DuckDB · dbt · Streamlit · Claude (Anthropic API) · Python
**Domain (example):** SAP MM — *Customer Premises Equipment (CPE)* procure-to-deploy analytics for a
fictional telecom operator, **Helios Telecom**.
**License:** MIT · **Status:** working MVP on synthetic data.

---

## What it is

Traditional data governance lives in expensive SaaS platforms (Collibra, Informatica) where the
business glossary, data catalog, and source-to-target mappings are locked behind a vendor UI and
invisible to automation. **DG AI Agent inverts that:** all governance metadata is plain text in git,
generated from code, and an LLM is a first-class actor that can read it, reason over it, and *build new
governed data products end-to-end*.

| Capability | Traditional tool | Here |
|---|---|---|
| Business glossary | Collibra / Informatica | `dbt/seeds/business_glossary.csv` → auto-generated wiki |
| Data catalog | Informatica scanner | `dbt/seeds/sap_data_dictionary.csv` + `schema.yml` |
| Source→Target mapping | Excel / Informatica | `dbt/seeds/s2t_mapping.csv`, **LLM-generated + verified** |
| Lineage | Informatica / dbt docs | dbt DAG + wiki lineage |
| Data profiling | Informatica DQ | live DuckDB queries (`scripts/`) |
| Business rules | Collibra / manual docs | transformation logic in wiki (plain-language + SQL) |
| Approval workflow | Collibra | `status` field (`draft → scope_confirmed → approved`) |

The differentiator: **a business term goes from one-line definition to a deployed, tested data mart
through an AI pipeline with deterministic guardrails** — not a human writing SQL.

---

## The AI Source-to-Target pipeline (the core)

A new metric ("CPE contribution margin by service plan and tenure") becomes a materialized,
grain-validated mart through staged LLM reasoning, where **the LLM proposes and deterministic checks
verify** at every step:

| Stage | What happens | LLM? | Guardrail |
|---|---|---|---|
| **0 · Spec** | human writes term: definition, grain, unit, filter | no | the contract for all later stages |
| **A · Scope derivation** | LLM picks the minimal SAP source tables from the data catalog | yes | proposed tables validated against the live DB; join paths checked vs **cardinality evidence** |
| **B · Blocker analysis** | gaps (unknown joins, missing data) become routed blockers | yes | blocker taxonomy with `resolves_in` routing |
| **C · Domain EDA** | profile each table (completeness, magnitude, code tables, PK/FK, grain) | mixed | LLM writes SQL, **DuckDB executes it** |
| **C′ · Term EDA** | validate *the term's* logic across tables (8-lens framework) | yes | per-blocker resolution gated on concrete evidence |
| **D · Generation** | LLM writes the dbt model SQL, citing the EDA evidence join-by-join | yes | RULE-3 layer + column pre-flights → **reject + repair-retry**; self-attestation audit |
| **E · Deploy** | `dbt run` / `dbt test` + semantic validator (grain/filter/unit) | no | the hard backstop — invalid SQL can't materialize |

Key principle, enforced in code: **ground it, verify it, never trust it.** The LLM is grounded with the
real schema, its output is checked against ground truth by deterministic pre-flights, and `dbt build`
refuses anything that wouldn't run.

---

## Architecture

```
Layer 0  RAW        SAP tables loaded into DuckDB (synthetic sample data)
Layer 1  STAGING    dbt views — clean / rename / type-cast SAP fields to business names
Layer 2  VAULT      dbt incremental — Data Vault 2.0 (hubs, links, satellites)
Layer 3  MARTS      dbt tables — Kimball star schema (dims, facts)
Layer 4  OBT        dbt views — flattened wide tables for BI
Layer 5  KNOWLEDGE  dbt views — computed business facts, KPIs, health checks
Layer 6  STREAMLIT  dashboard + governance UI reading from OBT / knowledge
```

**The knowledge graph** lives entirely in `dbt/seeds/` — `business_glossary`, `s2t_mapping`,
`known_decisions`, `known_issues`, `sap_data_dictionary`, `data_vault_design`, `org_structure`,
`abap_logic_catalog`, and more. The wiki under `knowledge/` is **generated** from these seeds
(`scripts/build_knowledge_wiki.py`); the source of truth is always the seeds + DuckDB, never markdown.

---

## Quickstart

```bash
# 1. clone + environment
git clone <your-fork-url> dg-ai-agent && cd dg-ai-agent
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. configure the LLM key (required for the AI pipeline)
cp .env.example .env        # then edit .env and set ANTHROPIC_API_KEY

# 3. generate the synthetic SAP dataset (creates cpe_analytics.duckdb)
python scripts/generate_sap_sample_data.py     # MM: PO, GR, inventory, equipment
python scripts/generate_zmm_approval_log.py    # ZMM: custom approval-log Z-table
python scripts/generate_sd_billing.py          # SD: customers, sales orders, billing
python scripts/generate_fi_shadows.py          # FI: accounting-document shadows

# 4. build the dbt layers  (run FROM the dbt/ dir — profiles.yml path is relative)
cd dbt && dbt seed && dbt run && dbt test && cd ..

# 5. launch the app
streamlit run app/Home.py
```

> **dbt note:** `dbt/profiles.yml` uses a relative DuckDB path (`../cpe_analytics.duckdb`), so always run
> dbt with the working directory set to `dbt/`.

---

## Repository structure

```
app/            Streamlit UI — dashboards + governance pages (glossary, catalog, data analysis)
                claude_api.py = the LLM pipeline (Stage A scope, EDA, Stage D generation)
dbt/
  models/       staging / vault / marts / obt / knowledge  (the 6 data layers)
  seeds/        the knowledge graph (glossary, s2t_mapping, decisions, catalog, ...)
scripts/        data generators, EDA analyzers, knowledge-wiki builder, session tooling
knowledge/      auto-generated governance wiki (do not hand-edit)
tests/          unit + regression tests
CLAUDE.md       agent operating instructions + architecture (read first if you use an AI agent here)
```

---

## How governance works in practice

1. A business owner defines a term in `business_glossary.csv` (or the Streamlit form).
2. The AI pipeline derives scope, runs EDA, and generates the S2T mapping + dbt model.
3. `dbt build` materializes and tests it; the semantic validator checks grain/filter/unit.
4. `scripts/build_knowledge_wiki.py` regenerates the human-readable wiki.
5. The owner reviews the wiki page (definition + source tables + transformation in plain language *and*
   SQL + data profile + lineage) and approves — `status` flips to `approved`.

Every analysis, decision, and issue is recorded back into the seed-based knowledge graph, so the
project's reasoning is itself version-controlled and queryable.

---

## Data & disclaimer

All data is **synthetic**, generated by the scripts in `scripts/`. **Helios Telecom** is a **fictional**
operator; the org structure, ABAP catalog, and SAP customizations are illustrative examples of *typical*
telco patterns, not any real company's system. Nothing here is proprietary or production data.

The AI pipeline calls the Anthropic API and **incurs token cost** on your own key. The repository never
contains an API key — `.env` is git-ignored; use `.env.example` as the template.

---

## License

MIT — see [LICENSE](LICENSE).
