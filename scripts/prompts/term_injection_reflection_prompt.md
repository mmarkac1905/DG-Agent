# Term Injection — Reflection Prompt

Runtime-loaded by `scripts/run_term_injection.py` (every
iteration, after mechanical gates). Runtime injects the same seven bundle
attestation sources as the iteration prompt, plus `{current_sql}`,
`{current_result_summary}`, `{gates_result}`, and
`{prior_iterations_summary}` at the markers below.

---

## REQUIRED BUNDLE SOURCES — ATTESTATION ECHO

Same seven as the iteration prompt (`ontology_consumed`,
`domain_facts_consumed`, `analysis_findings_consumed`,
`dar_consumed`, `prior_bar_consumed`, `semantic_model_consumed`,
`dbt_semantic_model_consumed`). Empty list `[]` is valid; null fails
attestation. `semantic_model_consumed` (Layer A) is the
list of raw `table_name` rows you consulted. `dbt_semantic_model_consumed`
(Layer B) is the list of dbt `model_name` rows you consulted.

## CITATION ID FORMAT

Same as iteration prompt: `DF-NNNN`, `AFNNN` (no hyphen),
`DAR-NNNNN`, `BAR-NNNNN`, `<model_name>`.

## CONSUMPTION DIRECTIVES

Same eight directives as iteration prompt (domain_facts, analysis_findings,
DAR ROWS, BAR ROWS, ontology, consumer priority, semantic model Layer A,
dbt semantic model Layer B). Your reflection uses the bundle to evaluate
whether the current SQL correctly consumed each source.

---

## YOUR TASK

Given the SQL from the current iteration and its mechanical result
(compile + run outcome, sample rows, row_count), reflect on:

**1. Checklist scoring.** For each term condition in `{term_conditions}`,
mark it as COVERED (SQL explicitly implements it), PARTIAL (SQL
implements a weaker or stronger version), or MISSED (SQL does not
implement or explicitly violates it). Provide a one-line evidence
pointer — the specific WHERE clause, GROUP BY, or JOIN that backs
your mark.

**2. Convergence signal.** Is the SQL converging vs the prior iteration
(if any), or diverging (getting longer, drifting topic, regressing)?

**3. Mechanical interpretation.** What does the mechanical result
tell you about term correctness? A compile-pass row_count=0 may be
correct (filter excluded everything) or wrong (filter matches nothing
because of a join bug). Decide whether the zero is justified.

**4. Scope sanity.** Does the frozen `scope_tables` list make
sense for this term's definition and notes? Answer yes|no|uncertain
with a one-line rationale. ("No" on two consecutive iterations will
hard-stop the session with `hard_stop_scope_mismatch`.)

**5. Shadow rubric (instrumentation, not used for stopping).**
Independently of the checklist, score the SQL on five 0-20 dimensions:
- **grain** (does SQL aggregate at the correct grain?)
- **scope** (does SQL use the correct tables for this term?)
- **filters** (does SQL apply all relevant WHERE clauses?)
- **aggregation** (is SUM/AVG/COUNT/ratio correct for the unit?)
- **joins** (are JOINs necessary and on correct keys?)

Emit both the `shadow_rubric_breakdown` dict (5 × 0-20) and the
`shadow_rubric_score` scalar (your sum). The runner recomputes the
scalar from the breakdown and flags arithmetic drift >3 points
(v3 P7 / F7 fix).

---

## OUTPUT FORMAT

Respond in **JSON only** — no free-form prose outside the JSON object,
no markdown headers, no explanations before or after the JSON. The
`reasoning_summary` field is the ONLY place for narrative content and
MUST be ≤ 80 words (hard cap):

```json
{
  "ontology_consumed": [...],
  "domain_facts_consumed": [...],
  "analysis_findings_consumed": [...],
  "dar_consumed": [...],
  "prior_bar_consumed": [...],
  "semantic_model_consumed": [...],
  "dbt_semantic_model_consumed": [...],
  "term_condition_assessment": [
    {"condition": "...", "status": "COVERED|PARTIAL|MISSED", "evidence": "..."}
  ],
  "semantic_alignment_score": <int 0-100>,
  "shadow_rubric_score": <int 0-100>,
  "shadow_rubric_breakdown": {
    "grain":       <int 0-20>,
    "scope":       <int 0-20>,
    "filters":     <int 0-20>,
    "aggregation": <int 0-20>,
    "joins":       <int 0-20>
  },
  "justified_zero": <bool>,
  "justified_zero_rationale": "one line, empty if row_count>0",
  "scope_sanity_answer": "yes|no|uncertain",
  "scope_sanity_rationale": "one line",
  "convergence_signal": "converging|stable|diverging",
  "reasoning_summary": "≤80 words on what you noticed about this iteration"
}
```

`semantic_alignment_score` = `covered_count / total_count × 100`, with
PARTIAL counting 0.5. If `total_count==0`, emit 50 (fallback).

---

**current_sql:**

```sql
{current_sql}
```

**current_result_summary:**

{current_result_summary}

**gates_result:**

{gates_result}

**term_conditions (frozen at preflight):**

{term_conditions}

**prior iterations (this session):**

{prior_iterations_summary}

Reflect now.
