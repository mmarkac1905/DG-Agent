# DG AI Agent — it learns your source system and writes the source-to-target mappings

**Point it at a source system. It profiles the schema, learns the relationships from the actual data,
then generates the source-to-target mappings and the transformation SQL itself — verified, deployed,
lineage-tracked.** A business metric goes from a one-line definition to a tested data mart with no human
writing SQL.

That is the part the catalog tools don't do. **Informatica and Collibra are passive repositories you fill
in by hand** — a person inventories the source tables, writes the S2T mapping in a spreadsheet, and
hand-codes the SQL. They don't *learn* a source system and they don't *generate* a mapping. DG AI Agent
does both, end to end, with every step checked by deterministic gates.

> **Stack:** DuckDB · dbt · Streamlit · Claude (Anthropic API) · Python
> **License:** MIT · **Status:** working MVP on 100% synthetic data
> **Example source system:** SAP MM — *Customer Premises Equipment (CPE)* procure-to-deploy, for a
> fictional operator, **Helios Telecom**.

> ⚡ **"Where's the data?"** There's no database in this repo — *by design*. The repo ships the
> **generators**, not a 250 MB binary. One command — `python scripts/bootstrap.py` — fabricates the
> synthetic SAP source system (deterministic, so everyone gets the identical dataset) and builds every
> layer locally. Nothing to download. See **[Quickstart](#quickstart)**.

---

## What's actually new here

Most "data governance" tooling — and most "AI + data" demos — stop at *cataloging* metadata or *chatting
about* data. The hard, unsolved part is **turning intent into a correct, deployable transformation**:
which source tables, how they join, whether the data even supports the metric, and what SQL computes it.
That requires **learning the source system from its data**, not just storing metadata about it.

| | Informatica / Collibra (catalog tools) | **DG AI Agent** |
|---|---|---|
| Inventory the source | manual data entry | **auto-profiled from the live database** |
| Discover joins · PK/FK · cardinality | not done (or hand-documented) | **empirically learned from the data (EDA)** |
| Source→target mapping | hand-written in Excel / a mapping UI | **LLM-generated, evidence-cited** |
| Transformation SQL | hand-coded by an engineer | **generated and build-tested** |
| Verify it's correct | manual review | **deterministic gates + `dbt test`** |

It *also* keeps the governance layer (glossary, catalog, lineage) as plain text in git instead of a vendor
SaaS — but that's the means, not the headline. The headline is **automatic source-system learning →
verified S2T mapping generation**.

---

## How it learns a source and builds the mapping — 7 stages

A business term becomes a materialized, grain-validated dbt mart through staged LLM reasoning where **the
LLM proposes and deterministic checks verify** at every step. Stages A–C′ are the *learning*; D–E are the
*generation + proof*:

| Stage | What happens | LLM? | Guardrail |
|---|---|---|---|
| **0 · Spec** | human writes the term: definition, grain, unit, filter | no | the contract every later stage validates against |
| **A · Scope** | LLM reads the catalog and picks the minimal source tables | yes | tables checked against the live DB; joins checked vs **cardinality evidence** |
| **B · Blockers** | gaps it finds become routed blockers (`→ C / C′ / human`) | — | deterministic triage |
| **C · Domain EDA** | **learns each table from its data** — completeness, magnitude, codes, PK/FK, grain | mixed | LLM writes the SQL, **DuckDB executes it** |
| **C′ · Term EDA** | validates *the term's own logic* across tables (8-lens agent loop) | yes | each blocker resolved only on concrete evidence |
| **D · Generation** | LLM **writes the S2T mapping + dbt SQL**, citing the EDA evidence join-by-join | yes | layer + column pre-flights → **reject + repair-retry**; self-attestation audit |
| **E · Deploy** | `dbt run` / `dbt test` + semantic validator (grain · filter · unit) | no | the hard backstop — invalid SQL physically cannot materialize |

The principle, enforced in code: **ground it, verify it, never trust it.** The LLM is grounded with the
real schema *learned in C/C′*, its output is checked against ground truth by deterministic pre-flights,
and `dbt build` refuses anything that wouldn't run — so the LLM's judgment is *useful* without being
*trusted*.

> **Worked example** (it lives in the repo). Define *"CPE contribution margin per service plan × tenure
> band, per month."* The pipeline learns a 14-table scope, profiles the real data, validates the term's
> logic, then generates a **vault-based dbt fact + a dashboard view** — which deploys grain-clean and
> reconciles billed revenue **to the euro**. Every decision, the EDA evidence, and the generated SQL are
> recorded back into the knowledge graph, so the *reasoning* is version-controlled too.

---

## The EDA framework — how it actually *learns* a source

The "learning" in stages C / C′ is two passes of structured exploratory data analysis. Together they turn
a pile of opaque SAP tables into the **evidence** the generator needs to write a *correct* mapping — so by
the time any SQL is written, the LLM is reasoning about *this* source system's measured shape, not an
abstract schema.

### Pass 1 — Domain EDA: profile each table on its own

A suite of analyzers runs per source table. Four are **LLM-assisted** (the LLM writes the profiling SQL,
DuckDB executes it, the LLM interprets the result); four are **deterministic** (pure SQL). Each answers
one question the mapping will depend on:

| Analyzer | What it measures | Why the mapping needs it |
|---|---|---|
| **completeness** | null rate per column | a measure that's 40% null needs `COALESCE`/a filter — or isn't usable |
| **dimensions** | distinct values / cardinality per column | finds the grouping axes and which columns are codes |
| **magnitude** | scale of each measure (sum, by dimension) | sanity scale + a reconciliation anchor (does it total what it should?) |
| **code_tables** | decode coded columns by **joining a decoder** (never a hallucinated `CASE`) | turns `BWART=101` / `MTART=CPE` into the meaning the filter logic needs |
| **date** | temporal span, gaps, granularity | confirms a `month` grain is even possible; finds missing periods |
| **segmentation** | value thresholds (quartiles) | banding for bucketed metrics |
| **schema_discovery** | PK / FK candidates + referential-integrity % | **discovers how the tables join** — the backbone of any S2T |
| **grain_relationship** | fanout class per table pair (`per_record_key` / `header_detail` / `catastrophic_fanout`) | tells the generator which joins are **safe** vs which would silently multiply rows and corrupt every aggregate |

The last two are the crux: the system **learns the join graph empirically** — from referential integrity
and cardinality in the *actual data*, not a hand-drawn ER diagram. That's how it avoids the classic
failure where a mapping joins two tables and silently 10×'s the revenue.

### Pass 2 — Term EDA: validate *this metric's* logic

Domain EDA learns the tables; Term EDA learns whether **your specific term** survives contact with the
data. It's a multi-turn agent that considers an 8-lens analytical framework and emits only the queries
that apply:

| Lens | Question it asks of the term |
|---|---|
| `measures_overview` | what are the headline totals / counts? |
| `by_dimension` | does the measure split cleanly by the chosen dimension? |
| `ranking` | top / bottom — any outliers that break the logic? |
| `time_trend` | does it behave sensibly over the date grain? |
| `cumulative` | do running totals make sense? |
| `variance` | actual vs target / prior — is the comparison meaningful? |
| `bucketing` | do the `CASE WHEN` bands populate? |
| `part_to_whole` | do the shares sum to 100%? |

It runs in three phases: **(1) framework floor** — consider all 8 lenses, pick or skip each *with a
reason*; **(2) reflection** — find the single biggest remaining gap and probe it; **(3) sufficiency
loop** — keep going until the evidence is enough to author a confident mapping, then stop. Every result
is recorded, so Stage D cites *real numbers* ("revenue sums to X; this join is 1:1") instead of guessing.

**Net effect:** when the LLM finally writes the S2T mapping and SQL, it already knows which columns are
populated, what the codes mean, how the tables genuinely join, and whether the metric holds up — because
it *measured* all of it first.

---

## Architecture

```
Layer 0  RAW        source tables generated into DuckDB (synthetic SAP sample data)
Layer 1  STAGING    dbt views — clean / rename / type-cast SAP fields to business names
Layer 2  VAULT      dbt incremental — Data Vault 2.0 (hubs, links, satellites)
Layer 3  MARTS      dbt tables — Kimball star schema (dims, facts)
Layer 4  OBT        dbt views — flattened wide tables for BI
Layer 5  KNOWLEDGE  dbt views — computed business facts, KPIs, health checks
Layer 6  STREAMLIT  dashboard + governance UI reading from OBT / knowledge
```

**The knowledge graph lives in `dbt/seeds/`** — both the governance content (`business_glossary`,
`s2t_mapping`, `sap_data_dictionary`, `data_vault_design`, …) and what the pipeline *learns* (per-table
analysis results, join/cardinality findings, semantic models). The wiki under `knowledge/` is generated
from these seeds; the source of truth is always the seeds + DuckDB.

---

## Quickstart

```bash
# 1. clone + environment
git clone <your-fork-url> dg-ai-agent && cd dg-ai-agent
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. (optional) LLM key — only needed to RUN the AI pipeline, not to build the data
cp .env.example .env        # then edit .env and set ANTHROPIC_API_KEY

# 3. build everything from scratch — generates the synthetic source system + all dbt layers
python scripts/bootstrap.py

# 4. launch the app
streamlit run app/Home.py
```

After step 3 you have a fully populated `cpe_analytics.duckdb` (~250 MB) and **135 dbt models** across
every layer — ready to query, dashboard, and run the AI pipeline against.

<details><summary><b>What <code>bootstrap.py</code> runs</b> (the same steps, by hand)</summary>

```bash
python scripts/generate_sap_sample_data.py   # MM:  PO, GR, inventory, equipment
python scripts/generate_zmm_approval_log.py  # ZMM: custom approval-log Z-table
python scripts/generate_sd_billing.py        # SD:  customers, sales orders, billing
python scripts/generate_fi_shadows.py        # FI:  accounting-document shadows
cd dbt && dbt seed && dbt run && dbt test     # build all layers (run FROM dbt/ — relative DuckDB path)
```
</details>

### Where the data comes from (no database is committed)

The repo ships **the data *generators*, the dbt models (DDL-as-code), and the seed knowledge graph** —
never the database, which is a build artifact (git-ignored). The generators are **deterministic**
(`seed=42`), so every clone produces the byte-identical source system; `dbt run` then recreates all 135
models. Think of the repo as the **recipe**, not the cooked meal — `bootstrap.py` cooks it locally. (The
generated **governance wiki under `knowledge/` *is* committed**, so you can browse the glossary, catalog,
and S2T mappings on GitHub without building anything.)

---

## Repository structure

```
app/            Streamlit UI + claude_api.py — the LLM pipeline (scope · EDA · S2T generation)
dbt/
  models/       staging / vault / marts / obt / knowledge  (the 6 data layers)
  seeds/        the knowledge graph — governance content + what the pipeline learns
scripts/        data generators, EDA analyzers, S2T pipeline runners, wiki builder, bootstrap.py
  prompts/      LLM prompt templates — one per pipeline stage/analyzer, loaded at runtime
knowledge/      generated governance wiki — committed so it's browsable on GitHub
tests/          pytest suite + fixtures
CLAUDE.md       agent & contributor guide (conventions for working in the repo)
```

---

## Data & disclaimer

All data is **synthetic**, produced by the generators in `scripts/`. **Helios Telecom** is a **fictional**
operator; the org structure, ABAP catalog, and SAP customizations are illustrative examples of *typical*
telco patterns — not any real company's system, and nothing proprietary.

The AI pipeline calls the Anthropic API and **incurs token cost on your own key**. The repo never
contains a key — `.env` is git-ignored; copy `.env.example` to start.

---

## License

MIT — see [LICENSE](LICENSE).
