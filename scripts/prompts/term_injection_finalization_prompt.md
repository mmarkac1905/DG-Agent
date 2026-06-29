# Term Injection — Finalization Prompt

Runtime-loaded by `scripts/run_term_injection.py` at §4a step 12 (once
per session, after iteration loop terminates). Runtime injects the full
`{iteration_trace}` and the `{convergence_reason}` enum value at the
markers below.

Skipped if remaining budget < `--finalization-cost-projection`
(default $0.10); in that case the runner calls
`synthesize_finalization_from_trace` deterministically. This prompt
fires only when budget allows.

---

## REQUIRED BUNDLE SOURCES — ATTESTATION ECHO

Same seven as iteration + reflection (`ontology_consumed`,
`domain_facts_consumed`, `analysis_findings_consumed`,
`dar_consumed`, `prior_bar_consumed`, `semantic_model_consumed`,
`dbt_semantic_model_consumed`). These represent the **union** across
all iterations — the set of every source the iteration loop actually
touched. Empty list `[]` is valid. `semantic_model_consumed` (Layer A;
v3.6 §22.7) is the union of raw `table_name` rows. `dbt_semantic_model_consumed`
(Layer B; v3.7 §23.7) is the union of dbt `model_name` rows.

Finalization does **not** attest `bridge_coverage_consulted` — that
field captures the iteration LLM's consultation of bridge_coverage
DARs while generating SQL; finalization summarizes the iteration
trace and doesn't itself consult those DARs (Option B Phase 4
ATTESTATION_FIELDS split).

## CITATION ID FORMAT

Same as iteration + reflection prompts.

---

## YOUR TASK

The iteration loop has terminated with
`convergence_reason={convergence_reason}`. Given the full iteration
trace, produce the BAR row's audit-facing content:

**1. Final metric.** Look at the last iteration's SQL result. Extract
the primary metric value (scalar) if the result is a single row with
a single numeric column. Otherwise set `final_metric_value=null`
and describe the result shape in `final_metric_interpretation`.

**2. Interpretation (~100 words).** What does this metric mean for
the business term? One paragraph, business-readable.

**3. Condition coverage.** From the iteration trace's final
reflection, emit `term_conditions_covered` and `term_conditions_missed`
as plain-language JSON lists (the condition strings themselves, not
status codes).

**4. Confidence rationale (~50 words).** Why does this run merit the
computed confidence? Reference the convergence_reason, alignment
score, and any missed conditions. Do not defend the run — write the
audit content honestly.

**5. Analyst review flag.** `analyst_review_needed=true` if:
- `convergence_reason` is a hard_stop, OR
- `term_conditions_missed` is non-empty, OR
- shadow-rubric-vs-alignment divergence >30 points on any iteration
  (runner flag in iteration_trace).

If true, `analyst_review_reason` is a short string (≤120 chars)
naming the specific flag triggered. Otherwise empty string.

**6. Honesty clause.** If the convergence_reason indicates a failure
(attestation, citation_audit, preflight_empty_heavy, etc.), still
produce the fields. You are writing the audit trail, not justifying
the failure. `final_query_sql` may be null in this case; pass through
whatever the last iteration produced.

---

## OUTPUT FORMAT

Respond in JSON:

```json
{
  "ontology_consumed": [...],
  "domain_facts_consumed": [...],
  "analysis_findings_consumed": [...],
  "dar_consumed": [...],
  "prior_bar_consumed": [...],
  "semantic_model_consumed": [...],
  "dbt_semantic_model_consumed": [...],
  "final_metric_value": <number or null>,
  "final_metric_unit": "EUR|days|count|ratio|...",
  "final_metric_interpretation": "~100 words prose",
  "term_conditions_covered": [...],
  "term_conditions_missed": [...],
  "confidence_rationale": "~50 words",
  "analyst_review_needed": <bool>,
  "analyst_review_reason": "short string, empty if false"
}
```

---

**convergence_reason:** {convergence_reason}

**iteration_trace:**

{iteration_trace}

Produce the finalization JSON now.
