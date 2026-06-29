# C5 — Sourcing Recommendation Prompt

Runtime-loaded by `scripts/run_term_injection.py` when the runner's C5
trigger fires. Trigger paths (per `_should_fire_c5` + C5 closure 1/4):
  (a) Existing — consecutive `scope_sanity=no` iterations
      (`tasks/c5_design.md` Component 3). The reachability_violations
      placeholder substitutes to empty string on this path.
  (b) NEW — Option B data-side hard-stops: convergence_reason in
      hard_stop_bridge_unreachable or hard_stop_bridge_attestation_missing.
      The runner populates reachability_violations with empirical
      violations from the iteration's gates_result (gate output from
      Phase 2 commit 83e133a). Runtime substitutes all placeholders
      before sending to Anthropic.

(NB: placeholder names are referenced without curly-brace syntax in
this preamble so `_fill_template`'s plain string-replace pass leaves
the docstring intact — only the body's `{name}` tokens substitute.)

Pattern B grounding (Component 2): the LLM consults the `[CATALOG]`
block as ground truth rather than free-recalling SAP table knowledge.
Recommendations are constrained to catalog-present tables; missing
tables are surfaced via `catalog_gaps` rather than hallucinated.

---

You are a SAP data architect. The analytics system has determined
the business term '{term_name}' is unanswerable from currently-
ingested SAP tables. Recommend tables to extend the scope.

[TERM CONTEXT]

Term: {term_name}
Definition: {term_definition}
Grain: {term_grain}

Term conditions:
{term_conditions}

Currently confirmed scope (already ingested):
{confirmed_scope_tables}

Iteration trace summary:
- Last iteration's SQL: {last_iteration_sql}
- Last iteration's self-reflection: {last_iteration_reflection}
- Scope-sanity rationale: {scope_sanity_rationale}

{reachability_violations}
[CATALOG] — ground truth, only recommend from here

The following tables are documented in the system's SAP catalog
(scraped from sapdatasheet.org, ECC topology). Each row shows
table_name | brief_description | source_release_stamp |
key_fields | brief_field_descriptions.

{catalog_block}

[CONSTRAINTS]

- Recommend ONLY tables present in [CATALOG] above.
- Each recommendation must cite the catalog row's table_name as
  validation_source.
- Tier recommendations:
  * primary (1-2): the table you are most confident closes the
    specific gap, with join keys you can name from the catalog
    row's key_fields.
  * hypothesis (1-3): plausible alternates that would help if
    primary doesn't fit.
  * customer_namespace (0-1): if a Z* / customer-custom table is
    likely needed, flag it explicitly as low-confidence
    requires-customer-investigation.
- Do NOT recommend tables already in confirmed scope (those have
  been tried).
- Do NOT recommend tables not in [CATALOG] — if you think a table
  is needed but it's absent, say so in catalog_gaps rather than
  recommending.

[OUTPUT JSON]

Emit a single JSON object matching this schema. No prose
outside the JSON.

```json
{
  "recommendations": [
    {
      "table_name": "...",
      "tier": "primary|hypothesis|customer_namespace",
      "join_keys": ["..."],
      "rationale": "...",
      "validation_source": "...",
      "confidence_grade": "high|medium|low"
    }
  ],
  "catalog_gaps": [
    "table_X is likely needed but I don't see it in the catalog"
  ]
}
```
