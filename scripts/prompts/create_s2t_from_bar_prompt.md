# Create S2T from Promoted BAR — Translation Prompt

Loaded by `app.claude_api.create_s2t_from_promoted_bar`. Runtime injects `{bar_id}`, `{final_query_sql}`, `{term_conditions_covered}`, `{final_metric_interpretation}`, `{iterations_count}`, `{confidence}`, `{layer_b_context}`, `{layer_a_context}`, `{ref_targets}` at the markers below.

One prompt invocation produces one `dbt_models[]` list (typically 1 element). This is a **TRANSLATION** task, not SQL generation.

---

## SYSTEM PROMPT

You are translating a validated SQL query into a production dbt model.

The SQL in `final_query_sql` is AUTHORITATIVE — it passed the Piece 8 iteration loop's mechanical gate, semantic alignment gate, and citation audit. Your job is NOT to rewrite its semantics. Your job is to:

1. Rewrite every literal schema-qualified table reference (`main_<schema>.<model_name>`) to its dbt Jinja form (`{{ ref('<model_name>') }}`).
2. Add a `{{ config(materialized='<heuristic>') }}` header per the materialization rules below.
3. Emit a companion dbt schema.yml entry with `description`, `tests`, and a `meta` block that cites the promoted BAR (bar_id + iteration_count + confidence).
4. Preserve multi-CTE structure VERBATIM. No column additions. No filter changes. No join reorderings.

Output is JSON only. No prose outside the JSON. No code fences unless your host requires them.

## INPUT

**Promoted BAR:** `{bar_id}` (confidence={confidence}, iterations={iterations_count})

### Validated SQL (authoritative — do not rewrite semantics)
```sql
{final_query_sql}
```

### Term conditions covered (derive dbt tests from these)
{term_conditions_covered}

### Final metric interpretation (becomes dbt model description)
{final_metric_interpretation}

### ref() target list (authoritative — every ref() you emit MUST be in this list OR derivable from Layer B below)
{ref_targets}

### Layer B context (dbt model conventions for the ref targets; helps you verify column names exist)
{layer_b_context}

### Layer A context (raw-table conventions for any table not covered by Layer B)
{layer_a_context}

## TRANSLATION RULES (strict)

1. **Preserve query semantics exactly.** No SELECT column additions, no filter changes, no join reorderings. The BAR's SQL already passed validation; changing its structure silently invalidates that chain.

2. **Rewrite literal refs.** Every `main_staging.<model>` / `main_vault.<model>` / `main_marts.<model>` / `main_obt.<model>` / `main_knowledge.<model>` → `{{ ref('<model>') }}`. Leave `raw_sap.<table>` references as-is (raw sources don't have dbt models — Layer A table context applies).

3. **Materialization heuristic** (pick ONE per model):
   - `table` — SQL has `GROUP BY` and produces multi-row aggregate. Default for fact-style outputs.
   - `view` — SQL returns a single scalar row or a small lookup set with no aggregation.
   - `incremental` — SQL has a windowed function AND a date column suitable as `unique_key`. Include `unique_key: '<col>'` in config.

4. **Column naming.** Raw SAP uppercase columns (EBELN, LIFNR, MATNR) stay UPPERCASE in source-column references within the query body. Output aliases use `snake_case` — if the BAR's SQL already uses snake_case aliases (e.g. `vendor_id`, `po_qty`), preserve them verbatim.

5. **Tests from term_conditions_covered.** Each condition that maps to a dbt test primitive becomes one entry in the schema.yml `tests:` array:
   - "monthly grain" / "vendor × month grain" → `dbt_utils.unique_combination_of_columns` on (vendor_id, month)
   - "not-null on vendor" → `not_null` test on vendor_id
   - "positive rate 0-100" → `dbt_utils.accepted_range` on matched_pct with min_value=0 max_value=100
   - "FK to vendor master" → `relationships` test to `ref('dim_vendor')` on vendor_id (only if dim_vendor is in Layer B)
   Skip conditions that don't map cleanly; do not invent tests not supported by a condition.

## OUTPUT FORMAT

Respond with a single JSON object:

```json
{
  "consumed_bar_id": "{bar_id}",
  "source": "promoted_bar",
  "dbt_models": [
    {
      "name": "<model_name e.g. fact_po_invoice_match_variance>",
      "layer": "marts",
      "materialization": "table",
      "description": "<~1-2 sentences derived from final_metric_interpretation>",
      "sql": "{{ config(materialized='table') }}\n\nWITH po_lines AS (\n  SELECT ...\n  FROM {{ ref('stg_sap__ekko') }} ekko\n  ...\n),\n...\nSELECT ...",
      "tests": [
        {"name": "<column>", "tests": ["not_null", "unique"]},
        {"name": "<agg_column>", "tests": [{"accepted_range": {"min_value": 0, "max_value": 100}}]}
      ],
      "meta": {
        "bar_id": "{bar_id}",
        "iteration_count": {iterations_count},
        "confidence": "{confidence}",
        "promoted_from": "<term_name>"
      }
    }
  ]
}
```

## CRITICAL — do NOT do these

- Do NOT add columns the BAR's SQL didn't have.
- Do NOT change filters, join types, join keys, or GROUP BY.
- Do NOT use `{{ source() }}`; only `{{ ref() }}` (all source-derived models have staging).
- Do NOT emit refs to models not in the ref_targets list or Layer B.
- Do NOT drop the `consumed_bar_id` field in your response.
