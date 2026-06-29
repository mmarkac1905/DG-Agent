# Post-Demo Roadmap (Stage F landing → BG027 demo → follow-up)

Captures work deferred from Stage F to preserve demo momentum. Each
theme has concrete scope + priority. Promote to active Stages
(Stage G / H / …) post-demo as feedback informs priority.

---

## Theme 1 — Piece 8 attestation + citation discipline

Surfaced in the Piece 9 / Piece 8 evidence crosswalk (see prior
diagnostics):

- **TAR attestation field.** Stage C TARs load into Piece 8's dynamic
  layer with rich narrative but the iteration prompt has no
  `tar_consumed` attestation field. LLM can use Stage C evidence
  without an audit trail. Add `tar_consumed` to ATTESTATION_FIELDS
  and teach `TAR-NNNNN` citation format alongside `DAR-NNNNN` /
  `BAR-NNNNN` / `DF-NNNN`.
- **`TAR-NNNNN` citation format not taught.** `_render_tar_section`
  emits IDs but Piece 8's citation format list (iteration prompt L27-39)
  omits them.
- **Deterministic-analyzer directives missing.** Directive #3 (DAR
  ROWS) covers 4 LLM analyzers only. The 4 deterministic
  (date/temporal_coverage, segmentation, grain_relationship,
  performance_baseline) land in the bundle with no instructions.
  Stage F adds a 5th (`schema_discovery`) — needs its own directive
  covering PK/FK/shape/bridge consumption.
- **Skipped-DAR semantics.** `status='skipped'` DARs (Stage D.1) sit
  in the bundle with `skip_reason` inside `result_json` but Piece 8's
  prompt never teaches the LLM to distinguish "analyzer couldn't
  apply" from "no evidence found."
- **Stage A blocker surfacing.** Entire `scope_derivation_history_json`
  (blockers + `resolves_in` routing) is invisible to Piece 8. LLM
  rediscovers from scratch.
- **Layer B scope filter returns staging-only for ontology-covered
  tables.** `_load_static`'s Layer B query filters
  `dbt_semantic_model` via `upstream_models LIKE '%raw_sap.{t}%'`, but
  `upstream_models` holds only direct upstream refs (depends_on.nodes).
  Vault / mart / OBT / knowledge models have staging in their
  upstream_models chain, not `raw_sap.*` directly — so the filter
  captures staging and nothing downstream. For BG027's scope
  (equi/mseg/mkpf/objk/mard), Layer B returns 5 staging rows and
  silently drops 47+ downstream models (hub_equipment,
  sat_equipment_general, link_equipment_material, dim_equipment,
  fact_goods_movements, obt_inventory_health, knowledge_*). Discovered
  alongside known_issue #74 during BG027 Create S2T prep — #74
  patched the `dbt_column_lineage` filter (ontology-layer fallback),
  which carries most of the missing signal, but Layer B itself remains
  staging-only for ontology-covered scopes. Architecturally worth
  revisiting: should Layer B's "which models touch this scope" signal
  use `dbt_column_lineage` (transitive lineage) instead of
  `upstream_models` (direct refs)? Deliberate pass warranted, not a
  mechanical patch.
- **§22.2 consumer-priority discipline — RESOLVED** via known_issue
  #79 (2026-04-24). Prior compile_semantic_model skip gate treated
  ontology coverage as a reason to skip Layer A, conflating the LLM-
  synthesized table-level narrative layer with the auto-generated
  structural lineage layer. Piece 8 consumes both layers independently
  for create_s2t / pre_s2t_reasoning (verified in
  LAYER_LOADERS dispatch). Refactor removed the skip gate; Layer A
  now compiles for any raw table with sufficient DAR coverage that
  isn't already human-protected. Preserved + DAR-incomplete skips
  remain as legitimate domain guards.

**Scope**: one Stage (4-5 commits) post-demo.

---

## Theme 2 — S2T Re-run archival flow

Deferred from Stage D.2 (known_issue #62). Safe re-run requires:
- Rename existing `dbt/models/<layer>/<target_model>.sql` into
  `dbt/models/archive/<ARC-ID>/`.
- Supersede `s2t_mapping` rows for the term before regeneration
  (new write pattern for Deploy handler — currently append-only).
- Rollback path if the new run fails.

Design doc required before implementation.

**Scope**: 1-2 commits. Small if archive policy is lifted from
existing archive_term mechanism.

---

## Theme 3 — SAP DD ingestion

Currently `sap_data_dictionary` is hand-authored (326 rows for ~42
tables). A proper ingest from SAP DD tables (DD02L, DD03L, DD04T)
would auto-populate field descriptions, data types, foreign key
declarations, and domain references. Benefit: fewer human-authored
guesses; true vendor metadata.

**Scope**: 1 commit to add the ingestion script + populate a
`sap_dd_ingested` flag on dictionary rows so hand-authored overrides
stay visible.

---

## Theme 4 — Analyst-authored catalog enrichment UI

Today `semantic_model` rows are either `populated_by='eda_compile'`
(LLM) or missing. Analyst has no direct edit surface — corrections
require CSV edit + dbt seed + page reload. A per-row "Override" UI
on Data Catalog's View B (per table) would let analysts edit
`typical_filters` / `common_traps` / `typical_use_cases` in place,
tagging the row `review_state='human_reviewed'` + `populated_by='analyst'`.

**Scope**: ~300 LOC on Data Catalog page + CSV write helpers.

---

## Theme 5 — Business Term Analysis tab enhancements

Deferred from earlier Stage plans:
- Deep-link from Stage A's scope confirmation into Term Scope tab
  with the confirmed term pre-selected.
- Surface `blockers_resolution` (from Stage C TARs) as collapsible
  per-term cards in Business Term Analysis.
- Grain Relationships section: currently term-scoped verification.
  Post-Stage-F, schema_discovery subsumes the discovery half — see
  known_issue #67 (Stage F reworded). Decide: keep as verification
  under term filters, OR remove entirely if term-scoped verification
  adds no signal.

**Scope**: 2-3 small commits.

---

## Theme 6 — Data Model Reconciliation

Stage F ships `schema_discovery` producing empirical FK evidence.
Stage F Commit 4 merges high-confidence schema_discovery FKs into
`semantic_model.typical_join_keys_json` via deterministic post-pass
(Option iv lite). Full reconciliation UI deferred post-demo.

### 6.1 — Authored vs Empirical diff on Data Model page
Data Model page currently renders from three sources merged equally:
hardcoded `SAP_RELATIONSHIPS` dict (26 raw_sap entries + 33 vault +
22 mart + 3 OBT), `dbt_model_relationships` seed,
`information_schema` guard filter. No provenance distinction, no
confidence badges. Post-demo: add toggle Authored/Empirical/Both
(Option ii from diagnostic) OR side-by-side diff view (Option iii).
- **Scope**: 250-400 LOC depending on option.
- **Requires**: `schema_discovery` DARs populated across most raw_sap tables.

### 6.2 — Surface referential integrity % on rendered relationships
Every ERD edge today renders identically. With schema_discovery
coverage, each empirical relationship carries an integrity % that
should surface as a badge ("mseg → mara: 100% integrity" vs
"mseg → ekbe: 97% integrity"). Distinguishes strong evidence from
conventions.
- **Scope**: ~100 LOC addition to Data_Model.py's JS renderer.
- **Requires**: `schema_discovery` coverage.

### 6.3 — Add `relationships:` tests to dbt schema.yml
`dbt_semantic_model.typical_join_keys_json` is empty across all 93
rows because the dbt project lacks `relationships:` schema tests.
Adding these would auto-populate the field via the existing
deterministic `compile_dbt_semantic_model.py` extractor. Low-hanging
fruit — no new code needed, just `schema.yml` enrichment per dbt
layer (staging/vault/mart).
- **Scope**: ~100 LOC in schema.yml.
- **Payoff**: dbt-layer relationships become discoverable by Stage A +
  Data Model page automatically.

### 6.4 — Migrate `SAP_RELATIONSHIPS` from Python to seed
26 raw_sap relationships currently hardcoded in `Data_Model.py`.
Should live in a `sap_relationships.csv` seed (mirroring
`dbt_model_relationships.csv` shape: `from_model, to_model,
relationship_type, column_mapping, etc.`). Enables schema_discovery
to override specific entries based on empirical evidence, plus makes
the authored set reviewable in git diffs.
- **Scope**: ~150 LOC (seed + schema.yml entry + migration of hardcoded dict).

**Priority post-demo:** 6.3 (lowest effort, immediate payoff), then
6.4 (unblocks 6.1/6.2), then 6.1 or 6.2 based on demo feedback.

---

## Theme 7 — Grain relationship deprecation decision

Stage F's `schema_discovery` includes sum-match evidence in
`relationship_shapes` for 1:N header-detail classifications, fully
subsuming `grain_relationship`'s discovery function (known_issue
#67). Evaluate post-demo:
- If term-scoped verification adds no signal: remove the
  `grain_relationship` analyzer entirely.
- If it does: relabel its Business Term Analysis tab section to
  "Term-Scoped Relationship Verification" and make it optional (not
  a Stage C prereq).

**Scope**: either zero-LOC decision + paper-trail OR ~60 LOC removal
+ Stage C prereq update.

---

## Theme 8 — Source Diagnostic UX polish

Minor UX improvements deferred from Stage F. None blocking; all
low-effort. Identified during post-routing-bug empty-state diagnostic.

### 8.1 — TL;DR banner for fresh tables
When View B renders for a table with zero DARs, add a single summary
line above the action buttons: "This table has no diagnostic evidence
yet. Click Run Source Diagnostic to populate." Reduces visual scan
time for analysts drilling into fresh tables.
- **Scope**: ~15 LOC.

### 8.2 — Disable Compile Semantic Model button when no DARs exist
Currently always-enabled; clicking pre-DAR would produce a confusing
error from `compile_semantic_model.py` (needs DARs to synthesize from).
Add `disabled=True` with tooltip "Run Source Diagnostic first" when no
success/skipped DARs exist for the table.
- **Scope**: ~10 LOC.

### 8.3 — Collapse empty analyzer grid into single info line
When all 7 analyzer entries are "⏳ no DAR yet," replace the 7-line
list with a single `st.info` message. Mixed-coverage tables still
render the full grid as today. Pure visual-noise reduction for fresh
tables.
- **Scope**: ~20 LOC.

All three are candidates for a single "empty-state polish" micro-commit
post-demo.

### 8.4 — Source Diagnostic bulk dispatch refinements

Beyond the subset multiselect (shipped pre-demo), consider:
- **"Exclude tables with fresh DARs" checkbox** to skip re-runs where
  `executed_at_utc` is within N days.
- **"Run only missing analyzers"** option to resume partial coverage
  rather than re-running all 7 analyzers.
- **"Run only schema_discovery"** partial-dispatch for bridge refresh
  without re-running the other 6.

Each is a small add, none demo-critical.
- **Scope**: ~30 LOC per item.

---

## Theme 10 — Schema type pinning

Discovered 2026-04-24 during the known_issue #73 DAR supersede
migration (commit d3e7030). When a VARCHAR column in a seed CSV is
all-empty across every row, DuckDB's type sniffer infers INTEGER
(with NULL values) rather than VARCHAR. The first actual string
write then fails with a conversion error and requires
`dbt seed --full-refresh` to drop and recreate the table with the
correct type. Not a bug per se, but a latent trap on any ID-ref or
supersede-style column that starts empty and later receives VARCHAR
values.

Already observed on `domain_analysis_results.superseded_by`. Same
risk is latent on:
- `s2t_mapping.superseded_by` (new column landing in Direction C
  Commit 6a — all-empty initially).
- `term_analysis_results.archive_ref` (new column, Direction C
  Commit 1b).
- `business_term_analysis_results.archive_ref` (ditto).

**Fix pattern:** add an explicit `config: column_types:` block to
each affected seed's schema.yml entry, pinning suspected-VARCHAR
columns. The `term_analysis_results` entry (schema.yml line 398+)
already uses this pattern — mirror it on `domain_analysis_results`
and on the new Direction C columns when they land.

**Preemptive treatment:** when Direction C Commits 1b + 6a land,
include the `column_types` declarations in the same commit —
avoids a one-time `--full-refresh` cycle on the first real write
to the new columns.

**Scope:** ~30 LOC across schema.yml (domain_analysis_results +
4 columns on future DC seeds). Zero runtime code change.

**Priority:** low. Workaround is `dbt seed --full-refresh`; cost
is one-time per migration. Bundle with Direction C Commits 1b /
6a as a small extension rather than a standalone post-demo commit.

---

## Meta: priority after BG027 demo

1. Theme 1 (Piece 8 attestation) — the biggest correctness gap.
2. Theme 6.3 (dbt relationships tests) — cheapest win.
3. Theme 2 (Re-run archival) — needed for real operational use.
4. Themes 4-7 — feedback-driven from demo observations.

Nothing here should block the BG027 demo from running. Every item is
a quality improvement on top of a working pipeline.
