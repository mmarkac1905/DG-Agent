# Option B Design Pass — bridge_coverage_by_filter analyzer + runtime gate

Closes the LLM-overconfident-yes architectural gap surfaced by
known_issue #100 (BAR-00005 in the Phase 2b validation runs). Adds a
deterministic, data-side check that complements C5's LLM-judgment-
based trigger.

This design doc is the Step 2 deliverable. Pre-implementation
experiments OQ-B (retrospective validation against BAR-00005) and
OQ-E (performance bound) gate any code change.

Written 2026-04-28. References `tasks/option_b_step0_report.md` for
structural reconnaissance citations and `tasks/c5_design.md` for the
sibling C5 component being extended.

> **Status update 2026-04-28:** Both pre-implementation experiments
> have run and **PASSED** (`tasks/option_b_oqb_results.md`,
> `tasks/option_b_oqe_results.md`). Five design refinements (F-1
> through F-5) were surfaced by OQ-B and are folded into the
> Component sections below as inline updates. The three open
> questions in the prior version of this doc are resolved per user
> decision and recorded inline. Doc not yet committed (per user
> decision: experiments-first, doc-second).

---

## Background and motivation

### The architectural gap (known_issue #100)

C5 (Phase 2b commit `3b12171`) catches the case where the LLM
*voluntarily concludes* "I can't answer this from current scope"
(`scope_sanity_answer == "no"` on two consecutive iterations →
`hard_stop_scope_mismatch` → C5 surfaces sourcing recommendations).

C5 does **not** catch the case where the LLM produces semi-plausible
SQL it considers valid on data that is structurally unable to answer
the term — the **LLM-overconfident-yes** case. BAR-00005 demonstrated
this:

- Term: BG027 (`cpe_active_deployed_count`)
- iter-1 SQL: filtered `mseg.BWART = '201'` (deployment movements) via
  the `seri` bridge (`equi → seri → mseg` joined on MBLNR/MJAHR/ZEILE)
- LLM self-evaluation: `alignment=90`, `scope_sanity=yes`,
  `compile=pass`, `run=pass`, `row_count_ok=True (justified_zero)`
- Empirical reality (per known_issue #99): `seri.MBLNR` only references
  BWART='101' (Goods Receipt) movements; deployment movements (BWART='201')
  exist in `mseg` at material-grain only (`mseg.SERNP='HT01'` sentinel
  across all 32K rows). The bridge is unreachable for BWART='201'.
- Outcome: SQL would have shipped 0 rows as "verified low" with the
  LLM's plausible interpretation. **The data didn't change between
  BAR-00003 and BAR-00005; the LLM's stochastic conclusion did.**

The discipline catches LLM "no" but not LLM overconfident "yes."

### The deterministic-data-side-check requirement

known_issue #100 explicitly: "the proper fix is the
bridge_coverage_by_filter analyzer (known_issue #98) plus a runtime
gate that performs deterministic data-side checks independent of LLM
judgment."

The gate must be:
- **Deterministic** — no LLM call, no stochastic component
- **Data-side** — derived from empirical raw_sap measurements, not
  from LLM training-data recall
- **Pre-execute** — fires before the runner spends DuckDB time on
  structurally-unanswerable SQL

### Why this is the right next stage

Option B is the data-side complement to C5. Together they close the
gap from both directions:
- C5 fires when the LLM admits ignorance → analyst gets sourcing
  recommendations
- Option B fires when the LLM *wrongly thinks* it knows → analyst
  gets a hard-stop with citation to the unreachability evidence

Both surface actionable analyst output rather than silent wrong
answers. This is the audit-discipline thesis (decision #73)
extended to the runtime gate.

---

## High-level architecture

```
┌─────────────────────────┐                     ┌───────────────────────────┐
│  schema_discovery       │── bridge_tables ──▶ │ bridge_coverage_by_filter │
│  analyzer (existing)    │   DAR result_json   │ analyzer (new, Comp 1)    │
└─────────────────────────┘                     └───────────────────────────┘
                                                              │
                                                              │  DAR rows
                                                              ▼
                                                ┌───────────────────────────┐
                                                │  domain_analysis_results  │
                                                │  table (analysis_type=    │
                                                │  "bridge_coverage_by_     │
                                                │   filter")                │
                                                └───────────────────────────┘
                                                              │
                                                              │  rendered into bundle
                                                              ▼
                                                ┌───────────────────────────┐
                                                │  context_assembler:       │
                                                │  _compact_bridge_coverage │
                                                │  _result (new, Comp 4)    │
                                                └───────────────────────────┘
                                                              │
                                                              │  in dynamic layer
                                                              ▼
                                                ┌───────────────────────────┐
                                                │  iteration LLM (sees DAR  │
                                                │  via directive #3 sub-    │
                                                │  bullet, Comp 3)          │
                                                └───────────────────────────┘
                                                              │
                                                              │  emits SQL
                                                              ▼
                                                ┌───────────────────────────┐
                                                │  run_term_injection.py    │
                                                │  iteration loop           │
                                                │                           │
                                                │  Gate 4 (ontology) ───┐   │
                                                │                       │   │
                                                │   ┌───────────────────┴─┐ │
                                                │   │ Gate 4.5 (NEW):     │ │
                                                │   │ bridge_coverage_    │ │
                                                │   │ gate (Comp 2)       │ │
                                                │   │                     │ │
                                                │   │ • parse SQL with    │ │
                                                │   │   sqlglot           │ │
                                                │   │ • extract bridge    │ │
                                                │   │   paths + filters   │ │
                                                │   │ • cross-check DARs  │ │
                                                │   │ • reject if filter  │ │
                                                │   │   unreachable       │ │
                                                │   └─────────┬───────────┘ │
                                                │             │             │
                                                │  Gate 5 (mechanical exec) │
                                                └───────────────────────────┘
```

Five components: analyzer (offline, produces DARs), DAR table
extension (analysis_type enum + accepted_values), runtime gate
(in-iteration parser + check), directive update (teaches LLM the
new constraint), bundle renderer (compacts the DAR for the LLM).

Plus a sixth concern: **orchestration** — when does the analyzer
get invoked? Phase 1 is hand-run (matches existing analyzer
patterns); Phase 2+ folds it into `end_of_task.py` or a make
target.

---

## Component 1 — bridge_coverage_by_filter analyzer

> **Refinement F-1 (from OQ-B):** "bridge" framing in this doc was
> loose. The analyzer's true input is **FK candidates** from
> schema_discovery DARs (`fk_candidates` array), not the 2-hop
> indirect-path enumeration in `bridge_tables`. Read every "bridge"
> in this Component as "FK-pair join restricted by the from-table's
> row population." The analyzer name is kept for continuity with
> known_issue #100, but the structure is FK-pair filter-reachability.
>
> **Refinement F-4 (from OQ-B):** layer mapping is trivial — staging
> tables are passthrough views over raw_sap. Analyzer measures against
> `raw_sap.*` (matches existing analyzer pattern). The runtime gate
> handles `main_staging.stg_sap__<X>` → `raw_sap.<X>` substitution.

### Algorithm

```
Input:
  - scope_tables: list[str]  (e.g. ["equi", "mkpf", "mseg", "objk", "seri"])
  - DuckDB connection to cpe_analytics.duckdb (raw_sap.* tables)
  - Optional: filter_column allowlist (Phase 1 hand-curated; OQ-D)

Step 1: Load bridge graph from schema_discovery DARs.
  - Query: SELECT result_json FROM main_seeds.domain_analysis_results
           WHERE analysis_type='schema_discovery'
             AND status='success'
             AND source_tables overlaps scope_tables
  - Parse result_json["bridge_tables"] for each row.
  - Build bridges: list[{between: [t0, t1], via: t_mid, path: str,
                         via_keys_t0_to_mid, via_keys_mid_to_t1}]
  - If no schema_discovery DARs for scope → SOFT FAIL with stderr
    warning. Don't write any bridge_coverage DARs. Operator must run
    schema_discovery first. (See Risk R1.)

Step 2: For each (bridge, filter_column) pair where filter_column
is in the allowlist AND exists in the FAR-end table (t1):
  - Issue DuckDB GROUP BY:
      SELECT t1.<filter_column>, COUNT(*)
      FROM raw_sap.<t0>
      JOIN raw_sap.<t_mid> ON <via_keys_t0_to_mid>
      JOIN raw_sap.<t1>    ON <via_keys_mid_to_t1>
      WHERE t1.<filter_column> IS NOT NULL
      GROUP BY 1
      ORDER BY 2 DESC
  - reachable_values = set of distinct <filter_column> values returned
  - all_distinct_values = SELECT DISTINCT <filter_column> FROM raw_sap.<t1>
  - unreachable_values = all_distinct - reachable

Step 3: Emit one DAR row per (bridge, filter_column) pair with
result_json shape per OQ-A below.
```

### result_json shape (resolves OQ-A)

**One DAR row per (bridge, filter_column) pair.** Each row is
self-contained (no dependencies on other bridge_coverage DARs to
interpret).

```json
{
  "bridge": {
    "from_table": "equi",
    "via_table": "seri",
    "to_table": "mseg",
    "via_keys_from_to_mid": ["EQUNR"],
    "via_keys_mid_to_to": ["MBLNR", "MJAHR", "ZEILE"],
    "schema_discovery_dar_id": "DAR-NNNNN"
  },
  "filter_column": {
    "table": "mseg",
    "column": "BWART",
    "data_type": "VARCHAR"
  },
  "reachable_values": [
    {"value": "101", "row_count_via_bridge": 18432}
  ],
  "all_distinct_values": ["101", "122", "161", "201", "501", "601", "...", "..."],
  "unreachable_values": ["122", "161", "201", "501", "601", "..."],
  "value_cardinality": {
    "all_distinct": 18,
    "reachable": 1,
    "unreachable": 17
  },
  "evidence_query_sql": "SELECT mseg.BWART, COUNT(*) FROM raw_sap.equi JOIN raw_sap.seri ON ... JOIN raw_sap.mseg ON ... GROUP BY 1 ORDER BY 2 DESC",
  "measured_at_utc": "2026-04-28T12:34:56Z",
  "measurement_method": "group_by_through_bridge",
  "rationale": "1 of 18 BWART values reachable via seri bridge; 17 unreachable. Filter on unreachable value will return 0 rows."
}
```

Field semantics:
- `reachable_values` — bounded by actual distinct count seen via
  bridge join. For BWART (~20 distinct codes), the list is small.
  For high-cardinality columns, see "Bounding" below.
- `all_distinct_values` — bounded by `LIMIT 1000` in the SELECT
  DISTINCT query. If that limit is hit, `value_cardinality.all_distinct`
  remains accurate (`COUNT(DISTINCT ...)`) but the displayed list
  is sampled. The allowlist explicitly prefers low-cardinality
  columns (≤100 distinct values typical), so the limit rarely fires.
- `evidence_query_sql` — the literal query that produced
  `reachable_values`. Stored for analyst-facing debugging and for
  the runtime gate's optional citation.

### Bounding for high-cardinality filter columns

The Phase 1 allowlist (OQ-D) deliberately restricts to low-cardinality
code-table columns (BWART, BSTYP, etc.) where enumeration is bounded.
For Phase 2+ candidates discovered via code_tables-DAR or scope-driven
selection, additional gates:
- If `all_distinct > 1000` → SKIP this filter_column for this bridge,
  emit DAR with `status="skipped"` and rationale "high cardinality".
- The runtime gate treats SKIPPED DARs the same as missing DARs for
  this (bridge, filter_column) — gate falls through silently for
  this filter (already covered by the existing SKIPPED-DAR semantics
  per directive #3 sub-bullet 10).

### DAR row construction

Mirror `scripts/run_schema_discovery_analysis.py`'s `_build_dar_row()`
template (lines 623–652). Copy `_DAR_FIELDS` verbatim (per decision
(h) — DAR writer duplication tolerated for this Stage).

| BAR field | Value |
|---|---|
| `id` | DAR-NNNNN via `_next_dar_id()` |
| `analysis_type` | `"bridge_coverage_by_filter"` |
| `executed_at_utc` | now |
| `result_json` | JSON shape above |
| `status` | `"success"`, or `"skipped"` for high-cardinality cases |
| `source_tables` | `<from_table>,<via_table>,<to_table>` (3 tables, csv) |
| `domain_name` | `"structural"` |
| `last_source_ingestion_at` | NULL (informational only) |

### Filter column allowlist (resolves OQ-D, Phase 1)

Phase 1 hand-curated list of code-table columns that appear in the
seeded raw_sap data and are commonly filtered:

| Column | Tables it appears in | Cardinality estimate | Rationale |
|---|---|---|---|
| `BWART` | mseg, seri | ~20 codes | Movement type — central to BG027 |
| `BSTYP` | ekko | ~5 codes | PO type |
| `BSART` | ekko | ~10 codes | PO category |
| `BLART` | bkpf, mseg | ~15 codes | Document type |
| `MTART` | mara | ~20 codes | Material type |
| `KTOPL` | t001 | ~3 codes | Chart of accounts |
| `LOEKZ` | ekpo, mara | 2 codes | Deletion flag (X / blank) |
| `STATU` | ekko | ~10 codes | PO status |
| `KOART` | bseg | ~5 codes | Account type (S/D/K/M/A) |
| `SHKZG` | bseg, mseg | 2 codes | Debit/credit indicator (S/H) |
| `XBLNR` | bkpf | high | EXCLUDED — too cardinal |
| `EBELN` | ekko, ekpo | high | EXCLUDED — natural key |

Phase 1 uses these 10 columns as the allowlist constant in
`scripts/run_bridge_coverage_analysis.py`. Phase 2+ extends:

- Phase 2: any column with a `code_tables` DAR is candidate. Detect
  via querying `domain_analysis_results WHERE analysis_type='code_tables'`
  and unioning the column refs from each DAR's `result_json`.
- Phase 3: scope-driven — parse term conditions to find candidate
  filter columns. Most ambitious; deferred.

### Performance bound (OQ-E confirmed empirically)

> **OQ-E result (2026-04-28): PASS by 750×.** Empirical timing on
> BG027 scope: 6 realistic measurements × 14.1 ms/pair = **0.08 sec**
> total. Per-pair budget was 600 ms; actual 14.1 ms (42× headroom).
> Realistic measurement count is 6 (not the worst-case 260) because
> only `mseg.BWART` matches the Phase-1 allowlist on the to-tables
> in BG027 scope. Full results in `tasks/option_b_oqe_results.md`.
> Phase 1 ships without optimization.

Estimate per BG027's scope:
- 5 scope tables → up to ~10 distinct bridges from schema_discovery
- 10 filter columns in the allowlist
- Worst case: 10 bridges × 10 filter columns = 100 measurements
- Each measurement = 1 GROUP BY + 1 COUNT DISTINCT = ~0.5–1 sec on
  the seeded data (~50K rows per major table)
- Total: 50–100 sec

**Acceptable upper bound: 60 sec.** OQ-E experiment (below) confirms
or revises before implementation.

Optimization knobs available if budget tight:
- Skip (bridge, filter_column) pairs where `filter_column` doesn't
  exist in the to_table (most pairs are no-ops).
- Cache per-bridge join results, then GROUP BY per filter_column on
  the cached result.
- Sample-based: SELECT ... USING SAMPLE 10% for cheap
  approximate reachability.

Phase 1 ships without optimization; Phase 2+ if needed.

### LOC estimate

~150–200 LOC, mirroring `run_join_cardinality_analysis.py`'s shape
but smaller (no cardinality classification, no bridge discovery —
those are pre-computed by schema_discovery). 12–15 tests.

---

## Component 2 — runtime gate

> **Refinement F-2 (from OQ-B):** multi-key joins (e.g., BAR-00003
> iter-2 uses `seri.MBLNR=mseg.MBLNR AND seri.ZEILE=mseg.ZEILE`
> jointly) need DAR-key matching logic. schema_discovery emits each
> FK as a separate candidate. Gate logic: find DARs whose
> `via_keys` are a SUBSET of the SQL's join keys; if any matching
> DAR shows the filter value as unreachable, the multi-key join is
> also unreachable (more keys can only narrow the result, never
> widen it). Conservative refusal: gate fires.
>
> **Refinement F-3 (from OQ-B):** IN-list semantics. Original spec
> only covered `=`. Add: `col IN (v1, v2, ...)` → refuse iff **all**
> values are unreachable; warn (don't refuse) if mixed. The literal
> equality rule (`col = v`, refuse if `v` unreachable) catches the
> BAR-00003 / BAR-00005 pattern even when an enclosing `IN` list is
> mixed; both filters tend to coexist in piece-8 SQL.
>
> **Refinement F-5 (from OQ-B):** sqlglot's default
> `find_all(exp.Join)` walks INTO CTE definitions. CTE-flatten work
> from `_s2t_cardinality_validator.py` is a fallback for complex
> patterns; not strictly required for the basic case. Phase 1
> ships with the default walk; F-5 mitigation is reused only if a
> later test surfaces a CTE pattern that breaks extraction.

### Insertion point

`scripts/run_term_injection.py` line 1652 / 1653 (between the
ontology collision check at lines 1634–1652 and the mechanical
gate at lines 1654–1675). Per decision (b).

The gate is **Gate 4.5** in the iteration pipeline (see Step 0
report area 3 for the existing 14-gate cascade).

### SQL parsing — sqlglot AST extraction (per decision (a))

Pattern-match `scripts/_s2t_cardinality_validator.py` (which already
uses sqlglot for an analogous post-generation cardinality validation;
shipped via commits 2b0ed87 / db6983c). The CTE-flatten work in
`db6983c` solves the BAR-00005 SQL pattern (CTE-wrapped multi-join);
reuse that flattening helper.

Extract from SQL:
- **Joins**: ordered list of (left_table, left_alias, right_table,
  right_alias, on_keys). Use sqlglot's `expressions.Join` walker.
- **Where filters**: list of (table_alias, column, operator, value)
  tuples for equality predicates. Limit Phase 1 to `=` and `IN`
  predicates (the dominant pattern in piece-8 SQL); skip range/LIKE
  for now.

### Reachability cross-check

Pseudo-code:

```python
def _bridge_coverage_gate(
    sql: str,
    scope_tables: list[str],
    conn: duckdb.DuckDBPyConnection,
) -> tuple[bool, list[str]]:
    """Returns (passed, violations).

    passed=True means: gate passes (no violations OR no DARs to
    check OR SQL unparseable per decision (g)).
    violations is human-readable, attached to iteration_trace.
    """
    # Step 1: load all bridge_coverage_by_filter DARs for scope
    dars = _load_bridge_coverage_dars(conn, scope_tables)
    if not dars:
        # Per decision (g) and OQ-C: skip gate, log warning, fall through
        sys.stderr.write(
            "[WARN] bridge_coverage gate: no DARs found for scope; "
            "schema_discovery + bridge_coverage_by_filter analyzers "
            "have not run for this scope. Gate skipped.\n"
        )
        return True, []

    # Step 2: parse SQL
    try:
        ast = sqlglot.parse_one(sql, dialect="duckdb")
    except sqlglot.errors.ParseError as e:
        # Per decision (g): fall through silently; mechanical gate
        # will catch unparseable SQL anyway
        sys.stderr.write(f"[INFO] bridge_coverage gate: SQL parse error: {e}\n")
        return True, []

    # Step 3: extract joins + filters
    joins = _extract_join_chain(ast)         # list[(t0, mid, t1, keys)]
    filters = _extract_equality_filters(ast)  # list[(table, col, value)]

    # Step 4: cross-check
    violations = []
    for (filter_table, filter_col, filter_val) in filters:
        # Find which bridge ends in filter_table within this SQL's joins
        bridge = _find_bridge_for_filter(joins, filter_table)
        if bridge is None:
            continue  # filter is on a directly-joined table or outside any bridge
        # Look up DAR
        dar = _match_dar(dars, bridge, filter_col)
        if dar is None:
            continue  # no measurement for this combo; soft fall-through
        if filter_val in dar["unreachable_values"]:
            violations.append(
                f"Filter {filter_table}.{filter_col}='{filter_val}' is unreachable "
                f"via bridge {bridge['from_table']}->{bridge['via_table']}->{bridge['to_table']}. "
                f"Reachable values: {dar['reachable_values']}. See {dar['_dar_id']}."
            )

    return (len(violations) == 0), violations
```

Routes back to the runner:

```python
# In run_term_injection.py at line 1652/1653:
gate_ok, bridge_violations = _bridge_coverage_gate(sql, scope_tables, conn)
if not gate_ok:
    convergence_reason = "hard_stop_bridge_unreachable"
    iteration_trace.append(_trace_entry(
        iter_num, sql, [], 0,
        gates={"bridge_coverage": "fail", "violations": bridge_violations},
        ...,
    ))
    break
```

### Missing-DAR policy (resolves OQ-C, hybrid option iii)

**Detection of "DARs exist for scope":** at least one
`bridge_coverage_by_filter` DAR row whose `source_tables` overlaps
the scope tables. Pure existence check; no per-bridge-pair
enforcement.

```sql
SELECT COUNT(*) FROM main_seeds.domain_analysis_results
WHERE analysis_type = 'bridge_coverage_by_filter'
  AND status = 'success'
  AND len(list_intersect(string_split(LOWER(source_tables), ','), ?::VARCHAR[])) > 0
```

If count = 0 → soft fall-through (warning only). Operator's
responsibility to ensure analyzer has run for the scope.

If count > 0 → engage the gate. Specific (bridge, filter_column)
pairs without matching DAR fall through silently (gate cannot
disprove what it doesn't have evidence for). Only rows where
`filter_value` is explicitly in `unreachable_values` cause a
hard-stop. **The gate is conservative** — it only refuses SQL
when it has positive evidence of unreachability.

### Warning surface (resolves the second half of OQ-C)

Three places:
1. **Stderr**: `[WARN] bridge_coverage gate: ...` for missing-DAR case
2. **iteration_trace.gates_result**: new field `bridge_coverage`
   with values `{"pass", "fail", "skipped_no_dars", "skipped_parse_error"}`
   so the BAR row's audit trail captures gate state per iteration
3. **convergence_reason** on hard-stop: `hard_stop_bridge_unreachable`

The runner's existing trim_note logic and analyst_review_reason
get a one-line addition that surfaces violations to the analyst.

### Parse-error policy (resolves decision (g))

`sqlglot.errors.ParseError` → soft fall-through to mechanical gate.
The mechanical gate (line 1654, full execute) will produce a
DuckDB error and route through existing `hard_stop_two_consecutive_mechanical`
or `hard_stop_mechanical_regression` logic. No new hard-stop type
for malformed SQL.

This keeps the existing pipeline shape intact. The bridge_coverage
gate adds a new failure mode (`hard_stop_bridge_unreachable`) but
not a new generic-SQL-failure mode.

### LOC estimate

~150–200 LOC total in run_term_injection.py + a new helper file
`scripts/_bridge_coverage_gate.py` for testability:
- `_bridge_coverage_gate.py`: ~120 LOC (extraction + cross-check)
- `run_term_injection.py`: ~30 LOC for the inline call + trace entry
- ~10 tests for the helper file pure functions
- ~5 tests for runner integration via mocked DAR set + mocked SQL

Total tests: ~10–12.

---

## Component 3 — directive update

### Sub-bullet content

Per decision (d), insert between current sub-bullet 9 (schema_discovery)
and sub-bullet 10 (STATUS=SKIPPED note) at
`scripts/prompts/term_injection_iteration_prompt.md` line 99/100.

Drafted text (matches existing directive #3 sub-bullet voice):

```
- bridge_coverage_by_filter DAR (per-bridge filter-value reachability)
  → if your SQL applies a filter `WHERE col = val` on a column whose
  reachability through the bridge you're using is unsupported by the
  evidence, your SQL will be rejected pre-execution by the runtime
  gate (hard_stop_bridge_unreachable). Cite the DAR in
  `bridge_coverage_consulted` (new attestation field, list of
  DAR-NNNNN ids). If a bridge_coverage DAR's `unreachable_values`
  list contains your filter value, either: (a) change the bridge
  (use a different join path), (b) change the filter (use a
  reachable value), or (c) acknowledge in `reasoning_summary` that
  the term is unanswerable from current scope and let the
  scope-sanity detector fire (twice → C5 sourcing recommendations).
  Do NOT proceed with SQL that the gate will reject — you waste an
  iteration.
```

### Position in directive #3

Sub-bullet 10 of the deterministic-DAR group (the group already has
5 sub-bullets — temporal_coverage, segmentation_threshold,
performance_baseline, grain_relationship, schema_discovery; this is
the 6th). The position keeps the order ANALYZER-driven (LLM
analyzers first, deterministic analyzers second, generic notes
last).

### New attestation field

`bridge_coverage_consulted: list[str]` — DAR-NNNNN ids the LLM
consulted. Symmetric with `join_cardinality_consulted` introduced
in Direction F (see app/claude_api.py:1013, 1099).

This requires:
- ATTESTATION_FIELDS list extension in `scripts/run_term_injection.py`
  to require this field (or accept it as optional — see OPEN
  QUESTION below)
- `_trace_entry` to persist it from response into the trace
- BAR schema column: `bridge_coverage_consulted varchar` (JSON
  list, nullable)

### LOC estimate

- Prompt template edit: ~12 LOC for the new bullet
- ATTESTATION_FIELDS update: ~3 LOC
- BAR schema migration: similar to Phase 2b's 5-column addition;
  ~30 LOC across `_bar_writer.py`, `schema.yml`, and the CSV
- Tests: 2 in `tests/test_iteration_prompt_directives.py`
  (sub-bullet present + position correct), 2 in
  `tests/test_bar_attestation_trace_union.py` (new field flows
  through trace + finalize correctly)

Total: ~50 LOC + 4 tests.

> **OQ-3a resolved (user decision 2026-04-28):** conditional. The
> LLM **always** emits the field as `[]` when empty (matching the
> existing 7 attestation fields' always-emit pattern). The runtime
> gate validates **conditionally** — required when DARs exist for
> scope, optional otherwise. Symmetric with OQ-C's hybrid missing-DAR
> policy: when no analyzer DARs exist, the gate (and attestation)
> degrade gracefully. When DARs exist, both engage.

---

## Component 4 — rendering

### Custom helper

`scripts/_context_assembler.py`, parallel to
`_compact_schema_discovery_result` (lines 978–1088).

Drafted shape:

```python
def _compact_bridge_coverage_result(raw_json: str, dar_id: str = "") -> str:
    """Render bridge_coverage_by_filter result_json as a compact
    BRIDGE-COVERAGE block. Skips DARs with status=skipped (high
    cardinality). Caps unreachable_values listing at 5 items.

    Output line format:
      BRIDGE-COVERAGE: {from}->{via}->{to} | filter: {table}.{col}
        reachable: [{val}, {val}, ...]
        unreachable: [{val}, {val}, ...] (+N more)
    """
    try:
        d = json.loads(raw_json)
    except json.JSONDecodeError:
        return raw_json[:200]
    bridge = d.get("bridge", {})
    fcol = d.get("filter_column", {})
    reach = [r["value"] for r in d.get("reachable_values", [])]
    unreach = d.get("unreachable_values", [])
    cap = 5
    unreach_disp = unreach[:cap]
    extra = len(unreach) - cap
    out = (
        f"BRIDGE-COVERAGE [{dar_id}]: "
        f"{bridge.get('from_table')}->{bridge.get('via_table')}->{bridge.get('to_table')} | "
        f"filter: {fcol.get('table')}.{fcol.get('column')}\n"
        f"  reachable: {reach}\n"
        f"  unreachable: {unreach_disp}"
        + (f" (+{extra} more)" if extra > 0 else "")
    )
    return out
```

### Type-switch dispatch addition

`scripts/_context_assembler.py:1240–1244` — append `elif` branch:

```python
elif str(r[1] or "").lower() == "bridge_coverage_by_filter":
    rendered_rj = _compact_bridge_coverage_result(
        r[4], dar_id=r[0] if has_id else ""
    )
```

### LOC estimate

- Helper: ~30 LOC
- Dispatch edit: 4 LOC
- Tests: ~6 in `tests/test_dar_render.py` covering reachable-only
  case, unreachable-truncation case, malformed JSON case, status=skipped
  case, missing-fields case, dar_id propagation case

Total: ~40 LOC + 6 tests.

---

## Component 5 — orchestration

### How the analyzer gets invoked

Phase 1: hand-run, mirroring existing analyzers:

```bash
python scripts/run_bridge_coverage_analysis.py --scope equi,mkpf,mseg,objk,seri
```

Or per-table:
```bash
python scripts/run_bridge_coverage_analysis.py --table mseg
```

(Adapter logic decides which bridges in scope have mseg as
from/via/to and runs measurements for each.)

### Load-order dependency

The analyzer **requires** schema_discovery DARs to exist for the
scope (it consumes their `bridge_tables` output as input). If
schema_discovery hasn't run, the analyzer soft-fails with a clear
message:

```
[ERROR] bridge_coverage_by_filter analyzer: no schema_discovery
DARs found for scope tables [equi, mkpf, mseg, objk, seri]. Run
schema_discovery first:
  python scripts/run_schema_discovery_analysis.py --table <each_table>
```

Hard-fail (exit code 1), don't write any partial DARs.

### Phasing

| Phase | Approach | Pros | Cons |
|---|---|---|---|
| **Phase 1 (this Stage)** | Hand-run | Matches existing pattern; minimal scope | Operator must remember |
| Phase 2+ | `end_of_task.py` integration | Auto-runs on every commit | More moving parts; `end_of_task.py` already has scan/sync/export logic — adding a per-scope analyzer requires deciding when "scope" is determined |
| Phase 3+ | Make target | Explicit, scriptable | Project doesn't currently use Make |

**Recommend Phase 1 = hand-run.** The runtime gate's missing-DAR
soft fall-through (decision (g) + OQ-C) makes this safe — the gate
silently no-ops if the analyzer hasn't run, with a clear stderr
warning. Phase 2+ tightens to require analyzer-has-run.

### LOC estimate

Phase 1 orchestration is **zero new LOC** (just CLI invocations).
Phase 2+ would add ~20 LOC to `end_of_task.py`.

---

## Pre-implementation experiments

Both experiments are **$0 cost, no LLM calls, deterministic**.
Both must pass before any implementation lands.

> **Status 2026-04-28:** both experiments PASSED. Full reports:
> - OQ-B: `tasks/option_b_oqb_results.md` — gate would refuse
>   BAR-00003 iter-2's SQL (BAR-00005 substrate); five design
>   refinements F-1 through F-5 surfaced and applied above.
> - OQ-E: `tasks/option_b_oqe_results.md` — 0.08 sec runtime on
>   BG027 scope (750× headroom).
> - The original brief tested against BAR-00005's SQL specifically;
>   that BAR row was deleted during Phase 2b commit prep
>   (architecturally-equivalent BAR-00003 iter-2 SQL substituted per
>   user-confirmed Option 1).

### OQ-B — Retrospective validation against BAR-00005

**Hypothesis:** A bridge_coverage_by_filter analyzer + runtime gate
implemented per this design would have caught BAR-00005's
overconfident SQL.

**Method:**

1. Reconstitute BAR-00005's `final_query_sql` from the iteration
   trace stored in DuckDB (or from the deleted-from-CSV BAR row,
   recoverable from git history if the validation log isn't enough).
2. Run schema_discovery for BG027's scope if not already done:
   `python scripts/run_schema_discovery_analysis.py --table seri`
   (and similarly for mseg, equi, objk, mkpf).
3. Manually compute (or implement minimal POC of) the analyzer for
   the (equi-seri-mseg, BWART) bridge: run the GROUP BY query on
   raw_sap, get `reachable_values` for BWART through that bridge.
   Expectation: `reachable_values = ["101"]`, `unreachable_values
   contains "201"`.
4. Manually parse BAR-00005's SQL with sqlglot, extract the
   bridge path and the BWART filter value.
5. Apply the cross-check logic: would the gate have rejected?

**Verdict criteria:**

- **PASS:** the gate would have rejected because `BWART='201'` is in
  `unreachable_values` for the equi-seri-mseg bridge. **Design
  validated; proceed to Component 1 implementation.**
- **FAIL:** the gate would NOT have rejected (e.g., the bridge
  path the LLM's SQL used isn't quite what schema_discovery would
  call a bridge; or the filter extraction misses the predicate).
  **Design has a gap; revise this doc before implementing.**

**Cost:** ~10 minutes Claude Code time; $0 LLM cost. The actual
DuckDB queries cost nothing on the seeded data.

### OQ-E — Performance bound

**Hypothesis:** The analyzer can complete a full BG027-scope run
in ≤60 sec.

**Method:**

1. For one (bridge, filter_column) pair on BG027's scope (e.g.
   equi-seri-mseg with BWART), time the GROUP BY query + the
   COUNT DISTINCT query.
2. Multiply by estimated total measurements (10 bridges × 10
   filter columns × overhead = ~100 measurements).
3. If single-pair time > 600ms, mark for optimization. If
   <600ms, total is well under 60 sec, design ships as-is.

**Verdict criteria:**

- **PASS** (single-pair < 600 ms): design ships per spec.
- **FAIL** (single-pair > 600 ms): triage before implementation —
  either narrow Phase 1 allowlist further, or implement caching
  optimization upfront.

**Cost:** ~5 minutes Claude Code time; $0 LLM cost.

---

## Phasing

| Phase | Scope | LOC | Tests | Cost |
|---|---|---|---|---|
| **0 (this doc)** | Design + experiments | 0 | 0 | $0 |
| **1** | Analyzer (Component 1) + schema.yml enum fix (latent SS-2) | ~150–200 + ~20 | 12–15 | $0 |
| **2** | Runtime gate (Component 2) | ~150–200 | 10–12 | $0 |
| **3** | Directive (Component 3) + renderer (Component 4) + BAR schema for new attestation field | ~80 | 6 | $0 |
| **4** | End-to-end validation: re-run BG027 with full Option B stack live; gate fires on any LLM SQL with BWART='201' through the seri bridge | 0 (validation) | 0 | $0.10–0.20 |

**Total LOC:** ~400–500. **Total tests:** ~28–33. **Total cost:**
~$0.10–0.20 for the final BG027 validation run.

Each phase is committed independently. Phase 4's validation either
shows the gate firing → demo-ready → close known_issue #100, or
surfaces a remaining gap → another design iteration.

---

## Risks and mitigations

| ID | Risk | Likelihood | Mitigation |
|---|---|---|---|
| **R1** | schema_discovery hasn't run for scope → analyzer can't produce DARs | medium | Component 5 orchestration: hard-fail with clear message; runtime gate's soft fall-through covers the analyst's "I forgot to run analyzer" case |
| **R2** | Filter-column allowlist too narrow → misses real cases | medium | Phase 2+ extension to code_tables-DAR-derived list; Phase 3+ scope-driven. R2 manifests as false-negatives, not false-positives — gate doesn't reject SQL it should accept |
| **R3** | SQL parsing edge cases (CTEs, nested subqueries, lateral joins) → bridge extraction wrong | medium | Pattern-match `_s2t_cardinality_validator.py`'s CTE-flatten work (commit db6983c). Reuse its test patterns. Add explicit tests for BAR-00005's SQL pattern, BAR-00003's SQL pattern, and edge cases observed in piece-8 BARs |
| **R4** | Analyzer performance regression (>60 sec on real scope) | low | OQ-E experiment validates upfront. Optimization knobs available (caching, sampling) in Phase 2+ |
| **R5** | False positives (gate refuses correct SQL) | low | Conservative gate logic — only refuses when filter_value explicitly in `unreachable_values`. Missing DAR → fall through. Unparseable SQL → fall through. Direct join → no bridge match → fall through |
| **R6** | LLM produces SQL the gate refuses on every iteration → max-iters with no progress | medium | The directive (Component 3) teaches the LLM about the gate before iter 1. If the LLM still hits the gate, it's a load-bearing finding (the term is genuinely unanswerable; C5's trigger should now fire on subsequent scope_sanity=no responses) |
| **R7** | New attestation field flow break (Component 3's `bridge_coverage_consulted`) | low | Phase 2b's 5-column BAR schema migration is the precedent. Same approach: extend BAR_COLUMNS, update BARStatus, schema.yml, _trace_entry. Pytest covers the trace flow |
| **R8** | Pre-existing latent enum issue (join_cardinality not in accepted_values) → masked by current dbt-test absence; surfacing it could fail on first dbt test run after Phase 1 | low | Decision (i): fix in same enum edit as bridge_coverage_by_filter addition. dbt test passes after Phase 1 lands |

---

## Decision log (anticipated for known_decisions.csv)

Captured at the time of each Phase commit:

| Anticipated # | Title | When |
|---|---|---|
| #86 | option_b_design_pass_completed | This doc lands |
| #87 | bridge_coverage_filter_column_allowlist_phase_1 | Phase 1 commit |
| #88 | bridge_coverage_oqb_validation_passed (or _revised) | After OQ-B experiment |
| #89 | bridge_coverage_runtime_gate_e2e_validated | Phase 4 commit |

Plus Decision references this doc inherits:
- #73 (audit-discipline thesis)
- #83 (attestation runner-bug fix)
- #85 (C5 pre-Phase-1 experiments)
- #86 (Phase 2b commit; structural verification only)

---

## Pre-implementation checklist

Items to satisfy before any code lands:

- [x] **OQ-B experiment passes** (gate would catch BAR-00005's SQL
  pattern; BAR-00003 iter-2 substrate confirms — see
  `tasks/option_b_oqb_results.md`)
- [x] **OQ-E experiment confirms ≤ 60 sec analyzer runtime** on
  BG027's scope (0.08 sec actual; 750× headroom — see
  `tasks/option_b_oqe_results.md`)
- [ ] Design doc reviewed by user (this document, post-update)
- [ ] Phase 1 spec drafted (after design review)
- [x] schema_discovery has run for BG027's scope (all 5 DARs
  present and consumed in OQ-B)
- [ ] Latent enum issue (SS-2) decision (i) confirmed: add both
  `bridge_coverage_by_filter` AND `join_cardinality` in same
  schema.yml edit

---

## Summary — what's filled in vs OPEN

### Filled in (decided in this doc)

- **Architecture** — analyzer + DAR + bundle + directive + gate
  pipeline, with diagram (above)
- **OQ-A (result_json shape)** — one DAR per (bridge, filter_column)
  pair, low-cardinality bias, full schema spec'd
- **OQ-C (missing-DAR policy)** — hybrid: hard-fail when DARs exist
  but contradict; soft fall-through when no DARs at all
- **OQ-D (filter column allowlist)** — Phase 1 list of 10 columns
  (BWART, BSTYP, BSART, BLART, MTART, KTOPL, LOEKZ, STATU, KOART,
  SHKZG); extension path to Phase 2+/3+
- **OQ-E (performance bound)** — ≤60 sec target; experiment plan
  validates pre-implementation
- **Pre-decided choices (a)–(i)** — all carried forward verbatim
  from Step 0 report
- **LOC + test count estimates** — per Component
- **Risk register** — 8 risks with mitigations
- **Phasing** — 4 phases with cost estimates
- **Pre-implementation experiments** — OQ-B and OQ-E with explicit
  pass/fail criteria

### OPEN — flagged for explicit user decision before Phase 1

- **OQ-3a (Component 3)**: Is `bridge_coverage_consulted` required
  attestation (hard-fail if missing) or optional (warning only)?
  Recommend conditional: required when DARs exist for scope,
  optional otherwise. User confirmation needed before Phase 3
  ATTESTATION_FIELDS edit.
- **Filter-column allowlist size for Phase 1**: Are 10 columns the
  right starting point, or should we trim to 3–4 to limit Phase 1
  surface? Recommend the full 10 — the analyzer cost per column is
  small and broader Phase 1 coverage front-loads value.
- **Should `tasks/option_b_design.md` itself be committed at this
  Stage, or only after OQ-B/OQ-E pass?** Mirroring c5_design.md,
  the design doc lands before experiments — but this doc explicitly
  notes that experiments may revise the spec. User preference:
  ship-now-revise-later vs experiments-first-doc-second.

### Validation that this doc is complete enough for Phase 1

- All five pre-decided choices applied: (a)–(i) ✓
- All five OQs from the user's brief have a recommended
  resolution: OQ-A ✓, OQ-C ✓, OQ-D ✓, OQ-E ✓ (experiment), OQ-B
  (experiment) ✓
- All five components specified at LOC + test count granularity
- Risks enumerated with mitigations
- Pre-implementation experiments scoped at $0 cost

Pure design pass. No edits to source code. No commits.

Cost: $0. Time: ~45 minutes Claude Code work.
