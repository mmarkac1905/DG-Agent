# Term Injection — Iteration Prompt

Runtime-loaded by `scripts/run_term_injection.py` (every
iteration). Runtime injects `{bundle}`, `{term_definition}`, `{term_notes}`,
`{term_conditions}`, `{scope_tables}`, `{prior_iterations_summary}`
at the markers below.

---

## REQUIRED BUNDLE SOURCES — ATTESTATION ECHO

Before generating SQL, you MUST emit these ten attestation fields
echoing what you observed in the bundle:

- `ontology_consumed`:       list of existing_models names seen
- `domain_facts_consumed`:   list of DF-NNNN ids seen
- `analysis_findings_consumed`: list of AFNNN ids seen (NO hyphen)
- `dar_consumed`:            list of DAR-NNNNN ids seen
- `prior_bar_consumed`:      list of BAR-NNNNN ids seen (term-scoped)
- `semantic_model_consumed`: list of semantic_model.table_name rows consulted (Layer A)
- `dbt_semantic_model_consumed`: list of dbt_semantic_model.model_name rows consulted (Layer B)
- `bridge_coverage_consulted`: list of DAR-NNNNN ids consulted from bridge_coverage_by_filter rows (Phase 3)
- `tars_consulted`:          list of TAR-NNNNN ids consulted from the TERM EDA section (C3)
- `stage_a_blockers_consumed`: list of `iter{N}.b{I}` ids consulted from the Stage A blockers section (C4)

If a source type has zero relevant items, emit `[]`, **not null**. A
null in any of these ten fields is a failed attestation and the
runner hard-stops the session with `hard_stop_attestation_failure`.

## CITATION ID FORMAT

Every ID you cite MUST be the primary-key string present verbatim in
the bundle text:

- `DF-NNNN`       — `domain_facts.fact_id` (4-digit, hyphen)
- `AFNNN`         — `analysis_findings.id` (3-digit, **no hyphen**)
- `DAR-NNNNN`     — `domain_analysis_results.id` (5-digit, hyphen)
- `BAR-NNNNN`     — `business_term_analysis_results.id` (5-digit, hyphen)
- `<model_name>`  — `existing_models` (e.g. `fact_purchase_orders`)

No slugs. No descriptions. If you cannot find the row in the bundle,
do NOT cite it.

## CONSUMPTION DIRECTIVES

**1. DOMAIN_FACTS.** If a domain_fact constrains the term's
grain/filter/unit, your SQL MUST reflect it. Example: DF-0003 says
"WAERS is 94% EUR, 6% HRK" and the term requires currency-normalized
aggregation → your SQL includes `WHERE WAERS='EUR'` or an explicit
conversion.

**2. ANALYSIS_FINDINGS.** If an AFNNN finding flags a data-quality
issue relevant to your SQL's columns, your SQL MUST handle it.
Example: AF005 says "MSEG.NETWR has 2% null" → your SQL includes
`WHERE NETWR IS NOT NULL` or an explicit COALESCE.

**3. DAR ROWS** (LLM analyzers + 5 deterministic types):
- Completeness DAR on a referenced column → handle nulls per the finding.
- Dimensions DAR on a grouping column → verify GROUP BY cardinality matches.
- Magnitude DAR with currency/unit awareness → your SUM/AVG MUST
  include the currency/unit partition the DAR identifies.
- Code Tables DAR → JOIN to the mapping table and surface the
  description column, do NOT leave coded values unmapped.
- temporal_coverage DAR on a date/timestamp column → use
  the min/max bounds when generating date filters. If
  gap_count is non-zero or null_pct is non-zero, your SQL
  MUST acknowledge them — either handle per the finding
  (NOT NULL guards, COALESCE, explicit boundary handling)
  OR include a one-line comment rationalizing why no
  handling is needed for this scope.
- segmentation_threshold DAR on a numeric column → use
  the empirical thresholds for any bucketing/segmentation
  logic on that column. Do NOT invent thresholds (round
  numbers, default percentiles) when data-driven
  thresholds exist.
- performance_baseline DAR (per-column min/max/avg/stddev/p25/p75)
  → use the bounds when generating numeric filters (avoid
  out-of-range thresholds); use p25/p75 to inform bucket
  boundaries. performance_baseline absent for a table means
  magnitude was skipped for that table — do NOT cite absence
  as positive evidence.
- grain_relationship DAR (1:1 / 1:N / N:1 classification
  with sum_match evidence) → match your GROUP BY / JOIN
  strategy to the empirical cardinality. For
  header_detail relationships with sum_match_pct, validate
  your aggregation produces sums consistent with the
  empirical match. Do NOT aggregate at a grain the
  relationship doesn't support.
- schema_discovery DAR (PK / FK / SHAPE / BRIDGE rendered
  structurally) → FK candidates with confidence=high are
  ground-truth join keys; use them and cite the DAR. FK
  candidates with confidence=medium are advisory; prefer
  high-confidence evidence when both exist. SHAPE entries
  inform whether your join is 1:1, 1:N, etc. — match your
  SQL to the shape. BRIDGE entries name the intermediary
  for indirect joins (e.g., three-way matching paths) —
  use the rendered bridge path verbatim; do NOT invent
  direct joins between bridged tables. If a direct join
  is empirically supported, it appears as an FK candidate
  in the schema_discovery output — use that instead. Do
  NOT hallucinate join keys when schema_discovery
  evidence exists for the table pair.
- bridge_coverage_by_filter DAR (per-bridge filter-value
  reachability) → if your SQL applies a filter
  `WHERE col = val` on a column whose reachability through
  the bridge you're using is unsupported by the evidence,
  your SQL will be rejected pre-execution by the runtime
  gate (hard_stop_bridge_unreachable). The gate handles
  CTE-wrapped SQL (it traces column lineage through
  subquery projections, so wrapping a bad filter in a CTE
  doesn't bypass the check). Cite the consulted DARs in
  the new `bridge_coverage_consulted` attestation field
  (list of DAR-NNNNN ids). If a bridge_coverage DAR's
  `unreachable_values` list contains your filter value,
  either: (a) change the bridge (use a different join
  path), (b) change the filter (use a reachable value),
  or (c) acknowledge in `reasoning_summary` that the term
  is unanswerable from current scope (the scope-sanity
  detector will fire on consecutive `no` and C5 will
  produce sourcing recommendations). Do NOT proceed with
  SQL that the gate will reject — you waste an iteration.
- DAR rows may carry STATUS=SKIPPED. A skipped DAR means the
  analyzer was attempted and ruled inapplicable for this scope
  (e.g. temporal_coverage on a table with no datelike columns).
  It is NOT equivalent to a missing or absent DAR — the analytical
  question was answered as 'not applicable here.' Treat skipped
  DARs as evidence that the corresponding analytical dimension
  is not relevant to this scope. Do NOT cite a skipped DAR as
  positive evidence; do NOT treat it as a coverage gap requiring
  further analysis.

**4. BAR ROWS** (this term's prior runs): if a promoted prior BAR
exists (`status='promoted'`), its `final_query_sql` is your starting
skeleton — refine it, do not rewrite from scratch. Cite BAR-NNNNN.
Unpromoted converged BARs are advisory only (consume but do not inherit
SQL).

**5. ONTOLOGY** (existing_models): if your proposed FROM/JOIN would
reference a table name matching an existing_models entry, use the
**literal table name** exactly as it appears in the bundle (e.g.
`FROM fact_purchase_orders`, qualified as `main_marts.fact_purchase_orders`
if the existing_models entry is in a non-default schema). **Do NOT
use Jinja `{{ ref() }}` syntax** — the mechanical gate runs raw
DuckDB, which does not render Jinja. **Do NOT emit `CREATE TABLE
<name>` or `CREATE VIEW <name>` where `<name>` matches an
existing_models entry** — collision with a production model is a
failure. The runner greps
your SQL for `CREATE TABLE/VIEW <name>` patterns and hard-stops
with `hard_stop_ontology_collision` if collision detected.

**6. CONSUMER PRIORITY (updated for Layer B).**
Three-tier priority for where to source per-table / per-model
conventions when writing SQL:
  1. **Layer B first** — if the scope table has dbt coverage, consult
     the dbt Semantic Model block (item 8). The LLM references the
     materialized model via its literal schema-qualified name, not
     the raw table.
  2. **Layer A second** — if the scope table is raw-only with no dbt
     coverage, consult the Semantic Model (Layer A) block (item 7).
  3. **Base SQL knowledge** — always available; used only when
     neither Layer B nor Layer A covers the requirement.
You will see this naturally: dbt-covered tables appear as Layer B
rows with their staging / vault / mart counterparts; raw-only tables
appear as Layer A rows. Layer A and Layer B are mutually exclusive
for a given scope table by design (Layer A compile skips
ontology-covered tables). Directive 5 (existing_models literal-name
rule) still applies — do not use Jinja `{{ ref() }}` in iteration SQL.

**7. SEMANTIC MODEL (LAYER A).** For raw tables in your
scope that lack dbt ontology coverage, consult the "Semantic Model
(Layer A)" block in the static layer. Use its `canonical_alias` when
aliasing the table (e.g. `FROM raw_sap.ekbe e` where `e` is the Layer
A canonical_alias for ekbe). Use `code_column_refs_json` to identify
which decoder seeds to JOIN against for code decoding; **never invent
inline `VALUES` clauses** for code tables — join the decoder seed the
Layer A row identifies. Apply `typical_filters` unless the term
definition explicitly overrides them. Cite the `semantic_model` rows
you consulted in the `semantic_model_consumed` attestation field
(list of `table_name` strings); emit `[]` if no Layer A rows were
relevant to your SQL.

**8. DBT SEMANTIC MODEL (LAYER B).** For scope tables
with dbt coverage, consult the "dbt Semantic Model (Layer B)" block
in the static layer. The block's `reference_sql` field is **already
rewritten to literal schema-qualified form** (e.g.
`FROM main_staging.stg_sap__ekbe e`) for this purpose — the raw
seed stores `{{ ref() }}` Jinja but the assembler rewrites it for
iteration consumers.
Per directive item 5, use literal schema-qualified references
(e.g. `FROM main_staging.stg_sap__ekbe e`) as shown in the Layer B
block. **Do NOT use `{{ ref() }}`** — the mechanical gate executes
raw DuckDB which does not render Jinja.
Use `exposed_columns_json` to verify column names before referencing.
Use `canonical_alias` when aliasing (e.g. `ekbe` for stg_sap__ekbe).
Use `typical_join_keys_json` to choose join conditions.
Cite the `dbt_semantic_model` rows you consulted in
`dbt_semantic_model_consumed` (list of `model_name` strings); emit
`[]` if no Layer B rows were relevant to your SQL.

## CITATION AUDIT

Every table name, column name, literal value, and ID cited in your
SQL must trace to the bundle text. The runner greps the bundle for
each identifier and hard-stops with `hard_stop_citation_audit_failure`
if any reference is unknown.

## TERM EDA ANALYTICAL CHARACTERIZATION

If the bundle contains a "Term EDA analytical characterization" section,
it represents Stage C's pre-computed analytical grounding for this term.
The section includes:
  - Sufficiency summary: lens consideration (8 lenses), confidence,
    declared_sufficient verdict, blocker resolution.
  - Query rows: analytical queries executed during Stage C, their
    results, and per-query interpretations.
  - Cited prior TARs: grounding evidence reused from other terms'
    Stage C runs, possibly with superseded annotations.

Use this as grounding evidence when composing the final S2T. Treat
the lens-level findings as established — you do not need to re-derive
them. Reuse query patterns (filter predicates, grain handling,
aggregation choices) where appropriate. Final S2T composition
remains your work.

When a citation note marks evidence as `[CITATION NOTE: ... is
superseded; historical evidence only.]`, prefer current-success
evidence on the same question where it exists. Superseded citations
remain useful as historical context but may not reflect current-state
truth.

**TAR CITATION DISCIPLINE (C3).** When you consult any TAR-NNNNN row
from the TERM EDA section above (whether `row_type='query'` or
`row_type='sufficiency'`), emit the consulted ids in the
`tars_consulted` attestation field as a list. Cite all TAR ids you
used while reasoning about SQL composition, grain, conditions, or
evidence. If you did not consult any TARs (e.g., because none were in
the bundle, or because the term's SQL didn't require analytical
grounding from prior characterization), emit an empty list `[]`.

Note that TARs include both current-term TARs and cross-term prior
TARs (from terms with overlapping s2t_mapping scope). Cite both as
needed; both contribute to the same `tars_consulted` field (TAR ids
are globally unique, so the runtime can derive the split from the
join to `term_analysis_results.term_id`).

## STAGE A BLOCKERS — KNOWN-CONCERN CONTEXT

The "Stage A blockers" section above (when present) documents
known-concerns identified during scope derivation that may affect
SQL composition. Each blocker has a `resolves_in` routing value:

- `domain_eda` / `term_eda`: upstream stages should have addressed
  these. Cross-reference with available DARs and TAR
  `blockers_resolution` to verify the blocker was resolved before
  treating it as closed.
- `analyst_decision`: human-only resolution; the SQL cannot resolve
  these. Acknowledge in `reasoning_summary` that these blockers
  remain unresolved and that the SQL is bounded accordingly.
- `ingestion_required`: blocked until new raw_sap data is ingested;
  the SQL is bounded by current scope. Acknowledge in
  `reasoning_summary` and do NOT fabricate columns or tables to
  work around the gap.
- `source_diagnostic_required`: semantic_model coverage warning;
  note in `reasoning_summary` if the warning is relevant to the
  SQL's grain or coverage.

**STAGE A BLOCKER CITATION DISCIPLINE.** When you consult any
blocker (whether to acknowledge it in `reasoning_summary`, to verify
upstream resolution, or to bound your SQL by its constraints),
emit the consulted blocker IDs in the `stage_a_blockers_consumed`
attestation field as a list. IDs follow the format `iter{N}.b{I}`
(e.g., `iter1.b0` for the first blocker of iteration 1) — copy them
verbatim from the section's per-blocker headers. If no blockers were
consulted (e.g., no blockers in bundle, or none pertinent to the
SQL), emit an empty list `[]`.

Pre-augmentation blockers — those rendered with the
`(unset (pre-augmentation))` tag in the `resolves_in` slot — predate
the routing taxonomy. Consult them if they describe a concern
relevant to the SQL; their lack of routing is informational, not
disqualifying.

---

## YOUR TASK

Given the term definition + term conditions (extracted by preflight) +
the bundle + any prior iterations + the frozen scope_tables, propose
the **next** SQL candidate implementing this term.

If this is iteration 1, start from scratch (or the promoted BAR's SQL
if one was cited). If iterations 2+, refine based on prior reflection's
COVERED/PARTIAL/MISSED assessment — address a MISSED condition or
sharpen a PARTIAL one.

Respond in **JSON only** — no free-form prose outside the JSON object,
no markdown headers, no explanations before or after. The
`reasoning_summary` field is the ONLY narrative surface and MUST be
≤ 80 words (hard cap):

```json
{
  "ontology_consumed": [...],
  "domain_facts_consumed": [...],
  "analysis_findings_consumed": [...],
  "dar_consumed": [...],
  "prior_bar_consumed": [...],
  "semantic_model_consumed": [...],
  "dbt_semantic_model_consumed": [...],
  "bridge_coverage_consulted": [],
  "tars_consulted": [],
  "stage_a_blockers_consumed": [],
  "query_sql": "SELECT ... FROM ...",
  "reasoning_summary": "≤80 words on why this iteration chose this SQL"
}
```

---

**bundle:**

{bundle}

**term_definition:**

{term_definition}

**term_notes:**

{term_notes}

**term_conditions (frozen at preflight):**

{term_conditions}

**scope_tables (frozen at runner start):**

{scope_tables}

**prior iterations (this session):**

{prior_iterations_summary}

Propose the next SQL candidate now.
