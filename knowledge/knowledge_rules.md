# CPE Analytics — Knowledge Rules & Anti-Patterns
# These are HARD RULES learned from real mistakes. Read before ANY work.
# Last updated: 2026-04-16

## RULE 1: dbt SQL is the SINGLE SOURCE OF TRUTH
NEVER write transformation_logic_sql in s2t_mapping.csv manually. Always:
1. Build the dbt model first
2. Run sync_s2t_from_dbt.py to extract ACTUAL SQL from dbt into the seed
3. Run sync_s2t_plain_from_dbt.py to generate business descriptions via LLM
4. Both scripts run automatically in end_of_task.py

If you add a new business term:
1. Add row to business_glossary.csv (definition, grain, owner)
2. Add row to s2t_mapping.csv — leave transformation_logic_sql EMPTY
3. Build the dbt model
4. Run end_of_task.py — it fills SQL and plain description automatically

## RULE 2: Column lineage comes from scanner, not assumptions
dbt_column_lineage seed has expressions for ALL 989 columns. If the S2T page shows a fallback to s2t_mapping, the issue is in the PAGE LOGIC, not the scanner. For direct pass-throughs, show "No transformation" — don't show fake SQL from s2t_mapping.

## RULE 3: Mart fact-dim relationships are LOGICAL, not in dbt ref()
Mart models only ref() vault models, not each other. For ERD, derive relationships using Option 3 — shared vault ancestors. If fact and dim both reference the same hub transitively, they are joinable. Do NOT hand-maintain a list. Do NOT rely on column name matching.

## RULE 4: Staging layer is MECHANICAL — no business logic
ONE model per source table. Keep ORIGINAL SAP column names. Only: hash keys, hashdiff, type casting, record_source, load_date. NO joins, NO business logic, NO derived fields. Business naming happens in VAULT satellites.

## RULE 5: sync_s2t_from_dbt.py skip rules
If dbt expression is a simple CTE reference (alias.column), skip. If entire trace chain is "direct", CLEAR the SQL field. Only overwrite when dbt has a REAL transformation (CASE WHEN, DATEDIFF, aggregate).

## RULE 6: Join labels on S2T page
Only show "Join:" when join_description contains "=" (actual join condition). Everything else is a source table description — hide it.

## RULE 7: Business Lineage vs S2T Specification — different audiences
Term Detail = business users (human names, plain English). S2T Specification = developers (actual SQL, technical lineage). Both derive from same dbt code.

## RULE 8: DV 2.0 structural rules
Every satellite MUST hang off a hub or link. If a satellite uses a composite hash key, there MUST be a link with that same hash key as PK. Verify with DV audit.

## RULE 9: Parquet export required after every change
Streamlit reads Parquet, not DuckDB directly. After any change: dbt seed, dbt run, export_parquet.py. Close DBeaver first or export fails.

## RULE 10: HTML component height estimation
Calculate height based on actual content. Add 20-30px safety margin, not 100px. Test at 100% zoom.

## RULE 11: Schema naming
main_ prefix on all dbt schemas. raw_sap has no prefix (Python-created). Do NOT refactor — too risky for zero benefit.

## RULE 12: ABAP section collapsed by default
Use st.expander with expanded=False.

## RULE 13: Data Model ERD — no "All tables"
Default to first process in list. Applies to ALL layers.

## RULE 14: When S2T mapping and dbt code disagree, dbt wins
The s2t_mapping seed is documentation — the dbt model is the code that actually runs. If a discrepancy is found, update s2t_mapping to match dbt, never the other way around. sync_s2t_from_dbt.py enforces this automatically.

## RULE 15: Column names change between layers — by design
SAP field LIFNR becomes vendor_id at the vault link level, not at staging. Staging keeps raw names (LIFNR). The rename happens in the vault SQL (e.g. `LIFNR AS vendor_id` inside link_po_vendor). The column lineage trace correctly follows these renames across layers. Do NOT expect the same column name at every layer.

## RULE 16: Calculated columns show multi-input convergence
When a column is derived from multiple upstream columns (e.g. lead_time_days = po_date - first_gr_date), the S2T Specification renders: one horizontal chain per input → JOIN/FILTER block → CALCULATION box (showing the formula from s2t_mapping.transformation_logic_sql) → OUTPUT box (the final mart column). Only render the CALCULATION box when multiple inputs converge — simple single-chain columns skip it.

## RULE 17: Unified view per column on S2T Specification
Each target column gets one expander with: business rule (plain language, left column) + actual SQL from dbt (right column, syntax-highlighted at 11px) + column-level lineage boxes (bottom strip). No separate sections for "Business Rule", "SQL", "Lineage" — they're all inside the same per-column expander.

## RULE 18: DBeaver uses in-memory DuckDB with Parquet
Streamlit and DBeaver should NEVER open cpe_analytics.duckdb directly — they read from data/parquet/ via in-memory DuckDB connections. If DBeaver holds a write lock on the .duckdb file, dbt seed and export_parquet.py will fail. Always close DBeaver before running the pipeline.

## RULE 19: Auto-learning — write rules DURING session, not after
Every bug fix that reveals a systemic pattern should produce a new rule in knowledge_rules.md and anti_patterns.md IMMEDIATELY, not at session end. The cost of forgetting a rule is repeating the bug. Write the rule while the context is fresh.

## RULE 20: Procedure for adding a new data source
1. Generate sample data (scripts/generate_sap_sample_data.py or new generator)
2. Create staging model (1:1 with raw table, SAP names, hash keys, hashdiff)
3. Create vault models (hubs, links, satellites as needed)
4. Create mart models (facts, dims joining vault primitives)
5. Run `dbt seed && dbt run && dbt test`
6. Run `python scripts/end_of_task.py` — this triggers scan_dbt_models → sync_s2t_sql → sync_s2t_plain → extract_relationships → export_parquet
7. Verify on Streamlit dashboard

## RULE 21: LLM-generated content needs hash-caching
sync_s2t_plain_from_dbt.py calls Claude API to generate plain-English descriptions. Without a cache, every end_of_task run would regenerate all descriptions (the LLM rewords each call), burning API credits and spamming git diffs. Use a sha256(input) → cache file (.s2t_plain_cache.json) and only call the API when the input hash changes.

## RULE 22: Technical deployment and business approval are separate concerns
Never automate changes to business_glossary.status. That field represents a human governance decision by the data owner. Technical actions (creating dbt models, running pipeline, syncing S2T mapping) can and should happen without approval — approval is a separate manual workflow step performed by the data owner through the glossary edit form or by directly editing the CSV with comments.

An "approved" flag set by code is worthless governance theatre — it bypasses the human review that the flag is supposed to represent.

Learned from: Session on 2026-04-16. User asked for atomic "deploy dbt models + save S2T" button. Implementation auto-set status='approved' unprompted, requiring a follow-up fix.

## RULE 23: DQ test results must come from execution, never hardcoded
Never show pass/fail icons (checkmarks, X marks) for data quality tests without actually executing the test SQL. Use the shared `execute_dq_test()` function for all DQ displays across all pages. A hardcoded green checkmark on a failing test is worse than no indicator at all — it builds false confidence.

Learned from: Session on 2026-04-17. DQ tab showed hardcoded green checkmarks for every test without running them. Term Detail tab correctly showed FAIL (146 violations) for the same test. Two pages disagreed on the same DQ result — a data trust issue.

## RULE 24: Domain facts are never hand-written into the seed
`dbt/seeds/domain_facts.csv` rows — specifically `fact_plain`, `fact_technical`, and `evidence_result_json` — MUST originate from a Guided Analysis — Domain execution where an `evidence_sql` query actually ran against the warehouse. Human curation is restricted to: status changes, `auto_inject` toggle, `priority_score`, `stale_after_days`, rejection, and light edits to fact wording AFTER the LLM's initial proposal.

Humans never invent facts without `evidence_sql` that was actually executed. A fact without reproducible evidence is indistinguishable from a guess.

Learned from: Session on 2026-04-17. Recon showed that structural observations (currency distribution, plant split, movement-type usage) were absent from every LLM prompt because there was no seed to put them in. Building the seed without this rule would invite hand-written prose that looks authoritative but cannot be re-verified.

## RULE 25: stale_after_days reflects volatility, not importance
For a new `domain_facts` row, set `stale_after_days` by the fact's rate of change, NOT its perceived business value:
- **Schema facts** (column exists, type is VARCHAR): `null` — never stales.
- **Cardinality facts** (N plants, N vendors, N materials): `90`.
- **Distribution facts** (currency %, plant volume shares, movement-type mix): `30`.
- **Null-baseline facts** (% of rows where NETWR IS NULL): `30`.
- **Temporal facts** (latest load date, fiscal-year coverage): `7`.

Importance is expressed via `priority_score` (1-100). Do NOT inflate `stale_after_days` on a low-value fact to hide it, and do NOT shrink it on a high-value one to force visible freshness. The two dimensions are orthogonal.

Learned from: Session on 2026-04-17. Without this rule, users will conflate "this fact matters" with "check it often," producing constant staleness warnings on stable schema-level facts and silent rot on volatile distributions.

## RULE 26: domain_facts is a shared knowledge base, not per-call context
`domain_facts` exists so every LLM call that plans or writes about the domain starts from what we already know — not from rediscovery. **Four injection points today**: S2T builder (`create_s2t_with_implementation`), Guided-BT planner (`run_data_analysis` from the BT tab), Guided-Domain planner (`run_data_analysis` from the Domain tab), Domain Report generator (`generate_domain_report`).

**Injection is scope-filtered, not content-filtered.** A BT term sees facts relevant to its `source_tables` (resolved from `s2t_mapping`). The goal is "relevant context", not "minimal context". Cutting facts too aggressively forces the LLM to waste a query slot re-discovering what we already know — more expensive than the extra prompt tokens.

**Non-inject sites** (the 5 other `claude_api.py` call sites + `sync_s2t_plain_from_dbt.py` with its 138-entry cache) are excluded because either their scope is too narrow for domain facts to help, or cache-churn economics make the swap unprofitable. These exclusions are specific — do not generalise them into "only inject into three/four specific sites forever."

The shared planner (`run_data_analysis`) is **mode-agnostic** — it accepts a `domain_context` string and prepends it with a standard header if non-empty. Callers compute their own context via `load_domain_context()` with appropriate `scope_tables` / `max_tokens` / `require_auto_inject`. Never branch inside the planner on caller identity.

Learned from: Session on 2026-04-17. Initial design excluded Guided-BT on "token budget" grounds. User pointed out this forces every BT analysis to re-discover domain trivialities (currencies, plants) in query plans. Correct framing is value-per-token.

## RULE 27: Streamlit's cached DuckDB connection must refresh parquet views on every query, gated by directory mtime
`@st.cache_resource` pins a single DuckDB `:memory:` connection across the Streamlit worker's lifetime. Parquet view registration happens once at connect time. CLI tools (`end_of_task.py`, `dbt seed`, manual exports) that write new Parquet files afterwards are **invisible to the cached connection** until the server restarts.

**Pattern to avoid:** assuming "dbt seed ran, therefore the table is visible". It is visible in the `.duckdb` file, but not in the in-memory connection Streamlit uses per Rule 18.

**Required pattern:** `query()` checks `data/parquet/` latest mtime, re-registers views via `CREATE OR REPLACE VIEW` when the directory advanced, and `DROP VIEW IF EXISTS` when a backing file has been removed. Steady-state cost: one `rglob` + `max(mtime)` — microseconds. Re-scan cost: ~160 × `CREATE OR REPLACE VIEW` — still sub-second. A module-level `_registered_views: dict[str, str]` tracks `view_name → filepath` so we only re-register on change, not on every scan.

**Diagnostic:** when a Streamlit query returns `"Table with name X does not exist! Did you mean Y?"` — and `X` exists as a seed/model in `cpe_analytics.duckdb` and as a Parquet file on disk — the cause is almost certainly a stale view cache, not a pipeline bug. The fix lives in `app/db.py`, not in the pipeline.

**Extension 2026-04-18 — module-reload drift detection.** Streamlit's file watcher re-imports `db.py` on any source byte change (a Phase edit, a `git add --renormalize .`, a whitespace tweak). Module-level globals `_registered_views` and `_last_view_scan_mtime` reset to their defaults (empty dict + `0.0`). But `@st.cache_resource`-cached `get_connection()` may survive the reload, leaving us with a DuckDB connection that **still has views registered** while the Python module's bookkeeping dict is empty. The reverse — fresh connection, stale dict — is also possible.

`_refresh_views` now probes the connection at the top of every call:

```python
conn_has_views = bool(conn.execute(
    "SELECT 1 FROM information_schema.tables "
    "WHERE table_type='VIEW' AND table_schema LIKE 'main_%' LIMIT 1"
).fetchone())
if (not _registered_views) and conn_has_views:
    _last_view_scan_mtime = 0.0       # module empty, conn has views → rescan
elif _registered_views and not conn_has_views:
    _registered_views.clear()          # conn stale, dict stale → rescan
    _last_view_scan_mtime = 0.0
```

On detected drift, the mtime gate is zeroed so the subsequent `_register_parquet_views` scan definitely runs, which in turn fires `query.clear()` (RULE 30) because `added > 0`. Both halves of the cache are brought back in sync on the next query.

Learned from: Session on 2026-04-17. Guided-Domain sanity run failed with *"Table with name `domain_facts` does not exist"* despite `dbt seed`, Parquet export, and Phase 6 verification all passing. Diagnosed as a stale view cache — the cached in-memory connection was built before `domain_facts.parquet` was first exported, and nothing refreshed its view catalog afterwards. The same symptom recurred post-Phase-12-hotfix: module reloaded, connection persisted, `_registered_views` was `{}` but DuckDB still had 161 views. Extension fixed by the drift probe above.

**Extension 2026-04-18 #2 — `close_connection()` as the authoritative reset.** Any function that signals *"clear Streamlit caches"* must **also clear the module-level bookkeeping** that tracks what's registered — otherwise the invariant *"state consistent with caches"* breaks, and the next `get_connection()` builds a fresh empty connection but `_register_parquet_views` sees `_registered_views` still claims to know all N paths → `continue`s every file → fresh connection has zero views. Four state locations, one invalidation signal:

1. `@st.cache_resource` on `get_connection()` — the in-memory DuckDB connection.
2. `@st.cache_data` on `query()` — result DataFrames.
3. `_registered_views` — module-level `dict[str, str]` of registered `schema.table → parquet path`.
4. `_last_view_scan_mtime` — module-level float gating `_refresh_views`.

`close_connection()` now clears all four. Hotfix 2's drift-detection in `_refresh_views` stays as the rescue net for the *other* trigger — Streamlit's file-watcher re-imports `db.py` independently of `close_connection()`, which resets 3 + 4 while `@st.cache_resource` may or may not preserve the connection. **Two scenarios, two safety nets. Fix at the signal source; keep the rescue for scenarios the signal can't cover.**

Post-Deploy flow before this fix: `close_connection()` clears 1 + 2 only → next query rebuilds connection but skips view registration (paths match the stale dict) → `_refresh_views` drift-detection fires on the NEXT query to rescue it. Catalog error was visible for one or two queries in the window. After this fix: `close_connection()` clears all four → next query does a true fresh scan → no drift window at all.

## RULE 29: Don't apply business-term governance to LLM context data
`business_glossary.status` has a human approval gate (RULE 22, anti-pattern #18) because business-term definitions are **official business deliverables**. `domain_facts`, by contrast, are data observations that the LLM uses as background knowledge to produce better S2T mappings, BT planner queries, and Domain Reports. No business owner approves *"EUR is 94% of POs"* — it is just a fact about the data.

**Do NOT copy** approval flows, draft-vs-active splits, or `auto_inject` toggles from the `business_glossary` pattern into seeds whose purpose is purely LLM context enrichment. One-touch delete is the only user control needed. Everything captured is `status='active'`, `auto_inject=true`, `discovered_by='guided_domain_llm'` from the moment it lands.

**Use this rule to decide UX** on future seeds: if the seed exists to feed downstream LLM prompts, default to zero-click save + one-click delete. If the seed describes something the business signs off on (glossary terms, SLAs, KPIs, dashboards that ship to stakeholders), the glossary pattern is appropriate.

Learned from: Session on 2026-04-17. Phase 9 first draft inherited `business_glossary` governance ceremony (18-field form, approval gate, reject/draft state). User correctly identified the pattern mismatch — `domain_facts` is not a business-approved artefact, so the gate adds friction without safety value. Reverted to zero-click capture + delete.

## RULE 30: Data-layer functions must not silently swallow errors
A `try`/`except` that returns an "empty but valid-looking" result (`pd.DataFrame()`, `[]`, `None`) from a query function is a **bug amplifier**. The original error gets lost; downstream code assumes the empty result is real, then fails far from the source with a confusing secondary error — a `KeyError` on a column that should be there, a cascade of empty plots, a silent dashboard full of zeros.

**Data-layer functions either succeed with real data or raise.** Callers decide locally whether empty is tolerable. Sites that genuinely need the empty-fallback behaviour wrap with their own `try/except` at the call site — that placement keeps the decision explicit and visible to the next reader, rather than hiding it behind the data layer's own silent safety net.

Related to RULE 27 (view cache refresh): these two rules are **two halves of one problem**. When the underlying data changes, both the view catalog AND the result cache must invalidate together. One without the other leaves a window where stale data propagates silently. The concrete wiring: when `_refresh_views` reports `added > 0 or removed > 0`, it also calls `query.clear()` on the `@st.cache_data`-decorated query function.

**Pattern to avoid** in any new data helper:

```python
def load_x(...):
    try:
        return do_the_thing()
    except Exception:
        return SomethingEmptyLookingLike(valid_data)  # ← bug amplifier
```

**Pattern to use:**

```python
def load_x(...):
    return do_the_thing()  # let it raise

# At the caller that can genuinely tolerate nothing:
try:
    x = load_x(...)
except SomeExpectedError:
    x = EmptyValue()
```

Learned from: Session on 2026-04-17. Post-Phase 8 view-refresh fix was incomplete — `@st.cache_data` kept a transient empty-DataFrame result alive for 60 seconds after the underlying view issue was fixed. BT tab crashed on `glossary['domain']` KeyError — 5 lines of stack trace from the real cause. Silent fallback in `db.py:query()` had converted a "table missing" error into a "column missing" error and hid the real problem for 2 minutes each time the cache refilled.

## RULE 31: Domain facts are valid only as long as the data hasn't changed
When new ingestion lands, **re-run every active fact's `evidence_sql`** and update or supersede the fact. Stale facts injected into LLM prompts poison downstream outputs — a Domain Report claiming "EUR dominates at 94%" when the real warehouse now has a 30/70 EUR/GBP split is worse than no context at all.

**Freshness gate is three-tier**:
- 🟢 **Green** (gap ≤ 3 days) — all operations allowed; no UI noise.
- 🟡 **Yellow** (gap 3-14 days) — operations allowed with a visible warning; the banner exposes a **"Refresh domain facts now"** button that runs the drift-detection workflow.
- 🔴 **Red** (gap > 14 days OR no ingestion baseline) — write-path actions are **blocked**: S2T build, Guided-Domain planning, Domain Report generation. Read-path (Guided-BT planner) stays enabled with a warning because stale context beats no context for reading analyses.

**Refresh workflow classifies three drift types** (`scripts/refresh_domain_facts.py`):
- **No drift** — identical records → `evidence_refreshed_at` bumped; zero LLM tokens.
- **Noise drift** — same structure, same set of primary-key values, dominant percentage within 5pp → focused LLM call rewrites `fact_technical` only (~400 tokens); `fact_plain`, `category`, `priority_score` preserved.
- **Material drift** — a new distinct value appeared in the primary grouping column OR the dominant-row percentage shifted more than 5pp → old fact marked `status='superseded'` with `superseded_by` pointing at a newly-minted row; new row produced by `interpret_domain_fact()` (~1100 tokens).

In a stable production environment target mix is roughly **70% no-drift / 25% noise / 5% material**.

**Recovery path**:
- Yellow → "Refresh domain facts now" button triggers the CLI refresh via subprocess.
- Red → refresh alone cannot fix the gap (the underlying data is ancient); banner instructs the user to re-run ingestion in a terminal.
- No baseline → same instruction: `python scripts/generate_sap_sample_data.py`.

Learned from: Session on 2026-04-17. User identified that domain facts derived from sample data would be wrong on real data — but the real insight is broader: any data change (even small, even on real data) makes prior facts suspect. Systematic drift detection plus a three-tier freshness gate solves both problems with one piece of plumbing.

## RULE 33: Prefer archive over delete for user-generated artifacts
Business terms, S2T mappings, dashboards, and any other user-authored artifact are **archived, not deleted**. Archived artefacts carry:

- a structured `archived_reason_code` (enum: `wrong_grain`, `bad_definition`, `redefined`, `obsolete`, `other`),
- optional free-text `archived_reason_text` (max 500 chars),
- full preservation of downstream rows (`s2t_mapping`, `analysis_findings`, `dbt_column_lineage`, etc.),
- `.sql` model files moved to `dbt/models/archive/<ARC-YYYYMMDD-NNN>/<layer>/` (excluded from dbt compilation via `dbt_project.yml`),
- a boolean `learning_signal` flag (default **TRUE**) controlling whether the archive feeds future LLM prompts.

**`learning_signal` decouples two questions:** *"was this archived?"* and *"should this influence future work?"*. Archives with `learning_signal=TRUE` surface to `create_s2t_with_implementation` via `app/_archive_context_loader.py` as a `## Previously archived attempts (learn from these failures)` block — the LLM sees the prior attempt's reason and target models when rebuilding the same name. Archives with `learning_signal=FALSE` are **invisible to the LLM** — appropriate for test runs, demo rehearsals, or any reproducible-state scenario where the next attempt must behave as if the archive never existed.

**Default is TRUE.** Users actively uncheck for the non-learning case. This matches the production use case (real archives teach future attempts) while supporting development workflows (demo re-records must be clean-slate).

**Composite uniqueness** on `(term_name) WHERE status != 'archived'` lets a new active term reuse a name whose prior instances are archived — without that rule, redefinition would require inventing new names. Enforced via a custom singular test at `dbt/tests/assert_business_glossary_term_name_unique_when_not_archived.sql`.

**Rule 22 exception:** the archive action mutates `business_glossary.status` from code, but only inside `app/archive_term.py:run_archive`, triggered exclusively by a user click on the Archive Confirm button. Rule 22's prohibition on automated status changes protects *technical-deployment* paths (S2T sync, dbt runs, pipeline steps) from accidentally flipping governance state — not explicit human actions.

**Exception to archive-not-delete:** truly transient artefacts with no business semantics (raw temp files, completely empty drafts that were never saved) can be hard-deleted. Anything with business semantics, any generated artefact, anything a reviewer might want to trace back to: **archive**.

Learned from: Session on 2026-04-17. Designed a demo-reset workflow; user redesigned as archive-not-delete to preserve audit trail and turn failures into LLM learning signal. The `learning_signal` flag emerged to separate the archive *action* from the learning *intent*.

## RULE 34: Line-ending discipline for seed CSVs has three layers
dbt's DuckDB CSV reader fails on **mixed CRLF/LF** line endings with a cryptic `"sniffer: 0 columns"` error. The project enforces LF-only seeds through **three layers of defense in depth**:

**Layer 1 — `.gitattributes` at repo root:**

```gitattributes
dbt/seeds/*.csv text eol=lf
dbt/tests/*.sql text eol=lf
dbt/models/**/*.sql text eol=lf
*.py text eol=lf
```

Removes Windows `core.autocrlf=true` as a silent CRLF source across every developer machine. Every git checkout, merge, or commit is normalised automatically.

**Layer 2 — Python writer convention.** All 15 seed-writing code paths use:

```python
with path.open("w", encoding="utf-8", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=..., lineterminator="\n")
```

or `df.to_csv(path, lineterminator="\n")`. Verified across `scripts/sync_s2t_from_dbt.py`, `scripts/sync_s2t_plain_from_dbt.py`, `scripts/refresh_domain_facts.py`, `scripts/scan_dbt_models.py`, `scripts/generate_sap_sample_data.py`, `app/archive_term.py`, `app/pages/Business_Glossary.py`, `app/pages/Data_Analysis.py`. This has been the documented convention since decision #7.

**Layer 3 — `end_of_task.py` pre-dbt-seed hook.** Before every `dbt seed --full-refresh` invocation, iterate `dbt/seeds/*.csv`, and `write_bytes(raw.replace(b"\r\n", b"\n"))` whenever `b"\r\n" in raw`. Logs which files were normalised. Catches any writer that slipped past Layer 2, any external tool that edited the CSV with CRLF, or any `Path.write_text` regression.

**`Path.write_text(string)` is a trap on Windows.** It opens the file in text mode and silently converts `\n` → `\r\n`. For any file that needs deterministic line endings — *especially seed CSVs* — use one of:
- `Path.write_bytes(string.encode("utf-8"))` (bypasses text-mode translation entirely), **or**
- `path.open("w", encoding="utf-8", newline="")` followed by `f.write(string)`.

Reserve `Path.write_text` for markdown / JSON / log files that are never parsed by DuckDB's CSV sniffer (`knowledge/*.md`, `context/*.md`, `.s2t_plain_cache.json` — all already using it safely).

Learned from: Session on 2026-04-17. The archive-workflow verify harness consumed hours of debugging because `subprocess.run(..., "dbt seed ... archive_log")` failed with `"sniffer: 0 columns"`. Root cause: the harness's `finally{}` restore used `SEED_GLO.write_text(orig_glossary, ...)` which silently wrote CRLF on Windows; the subsequent run_archive wrote with LF; the resulting mixed-line-ending file confused DuckDB's CSV dialect sniffer. Production code was correct throughout — the single broken line was in the test harness. That convinced the team to add defense layers 1 and 3 rather than rely on layer 2 alone.

## RULE 35: pandas NA breaks truthiness checks
`pd.NA` — the nullable scalar used by pandas `string`, `Int64`, `boolean` extension dtypes — **raises `TypeError: boolean value of NA is ambiguous`** whenever it's evaluated in a boolean context. This includes:

- `value or fallback` (our most common trap)
- `if value: ...`
- `not value`
- `value and other`
- `bool(value)`

**Anti-pattern (never write this on a pandas row):**

```python
x = term_row.get('business_join_description', '') or ''     # BOOM on pd.NA
if term_row.get('notes'): ...                                # BOOM on pd.NA
desc = str(row.get('description', '') or '').strip()         # BOOM on pd.NA
```

**Safe pattern — use `pd.notna` guards:**

```python
raw = term_row.get('business_join_description', '')
x = str(raw).strip() if pd.notna(raw) else ''

raw_notes = term_row.get('notes')
if pd.notna(raw_notes) and str(raw_notes).strip():
    ...
```

**What is a "pandas row" in this codebase?** Any variable produced by `df.iterrows()`, `df.iloc[i]`, `df[mask].iloc[0]`, or a row passed as a parameter to a helper that originated from one of those. Typical local names: `term_row`, `s2t_row`, `fact_row`, `f_row`, `cl_row`, `row`, `r`, `_r`, `_saved_meta`, `dv`, `contract`, `prog`, `zt`, `term`, `ent`, `peer`, `m`.

**What is NOT a "pandas row"** (and where `x or fallback` is safe):
- LLM JSON-response dicts like `analysis_plan.get("exploration_queries", []) or []` — these are plain Python dicts produced by `json.loads`; values are never `pd.NA`.
- `csv.DictReader(f)` iteration — values are always strings or `None`.
- `st.session_state.get(...)` — regular Python dict.

Classify each `x or fallback` site before "fixing" it. Rewriting LLM-dict sites to use `pd.notna` is pure churn.

**Don't hide it behind a top-level helper yet.** The inline `raw = ...; x = str(raw).strip() if pd.notna(raw) else ''` pattern is 2-3 lines and keeps the NA check visible at the call site. Extract a helper only if the same pattern shows up in 20+ places in one file — at which point the helper name documents the intent rather than obscuring it.

Learned from: Session on 2026-04-17. Mid-demo-rehearsal, the Business Glossary page raised `TypeError: boolean value of NA is ambiguous` on multiple tabs — Term Detail, S2T Specification, Data Quality. Root cause was 28 sites using `row.get('col', '') or ''` on pandas-extension-dtype-string cells where the CSV row had an empty cell, which pandas parsed as `pd.NA`. Project-wide sweep replaced every pandas-row instance with the `pd.notna`-guarded pattern. The ~30 LLM-JSON-dict sites using the same syntactic pattern were left alone because they cannot produce `pd.NA`.

## RULE 36: pandas.Timestamp is not a drop-in for datetime
`pd.Timestamp` inherits from `datetime.datetime`, so `isinstance(ts, datetime)` returns **True** — but the two share method *names*, not *behaviour*, for timezone operations. The specific trap:

```python
# Fails when dt is a pd.Timestamp:
local = dt.replace(tzinfo=timezone.utc).astimezone()
# TypeError: tz_convert() takes exactly 2 positional arguments (1 given)
```

`Timestamp.replace(tzinfo=...)` delegates internally to `tz_convert`, which pandas implements with different arity than stdlib `datetime.replace`. Chaining `.astimezone()` afterwards hits the pandas implementation and raises.

**Where this bites:** data read from Parquet comes as `pd.Timestamp`; data from `datetime.now()` or an `.isoformat()` parse comes as `datetime.datetime`. Any helper that accepts "a UTC-ish timestamp" from a mixed pipeline — e.g. `freshness._fmt_local`, `freshness._to_utc_naive` — must assume the input could be either type.

**Safe pattern — normalise first, then stay in stdlib-datetime land:**

```python
if dt is None:
    return "never"
try:
    if pd.isna(dt):
        return "never"
except Exception:
    pass
if isinstance(dt, pd.Timestamp):
    dt = dt.to_pydatetime()          # drop to plain datetime
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)   # attach UTC (stdlib semantics)
local = dt.astimezone()              # convert to local (stdlib semantics)
return local.strftime("%Y-%m-%d %H:%M")
```

Split tz-attach from tz-convert into two discrete operations. Never rely on `dt.replace(tzinfo=X).astimezone()` chained on a value that might be a `Timestamp`.

**Alternative (stay in pandas):** use `Timestamp.tz_localize(X)` to attach a tz and `Timestamp.tz_convert(Y)` to convert between tz's. This keeps the full operation inside pandas, which is fine — but only if you KNOW the input is a `Timestamp`. The normalise-to-datetime-first pattern is safer for mixed-input helpers.

**Also beware:** `pd.isna(dt)` catches both `pd.NaT` and `pd.NA`. Check it *before* testing `dt is None`, because `pd.isna(None)` also returns True but is cheaper to short-circuit on.

Learned from: Session on 2026-04-18. The freshness banner on Guided-Domain and BT tabs raised `TypeError: tz_convert() takes exactly 2 positional arguments (1 given)` mid-demo. `_fmt_local` in `app/freshness.py` was written for a stdlib datetime but received a `pd.Timestamp` from a Parquet-read `MAX(finished_at_utc)` path. Fixed by adding `isinstance(dt, pd.Timestamp): dt = dt.to_pydatetime()` normalisation at the top of both `_fmt_local` and `_to_utc_naive`, plus an upfront `pd.isna(dt)` check to handle `NaT` cleanly.

## RULE 37: Guard critical seeds at the write boundary
When forensics after a catastrophic seed truncation cannot identify the exact wiping code path, **do not** hunt every `pd.read_csv → mutate → pd.to_csv` roundtrip site in the codebase. Guard at the write boundary instead — one helper, one import, one call per writer.

`app/_csv_safeguard.py` exposes `assert_csv_safe(path, df)` (DataFrame variant) and `assert_csv_safe_row_count(path, n)` (count variant for append-mode writers that never materialise a full DataFrame). Both consult `SAFEGUARDED_SEEDS`, a per-seed rule dict:

```python
SAFEGUARDED_SEEDS = {
    "s2t_mapping": {
        "min_rows_absolute": 30,   # absolute floor — below is definitionally corruption
        "max_delete_per_op": 10,   # max rows removed per single write
    },
}
```

Two checks per call:
1. **Absolute floor** — writing below `min_rows_absolute` raises. Tune the floor below the lowest legitimate post-write size you expect to ever see.
2. **Delta ceiling** — if the existing on-disk file is readable, writing a DataFrame that would remove more than `max_delete_per_op` rows raises. Tune it to accommodate normal operations (Rule 14 LLM-hallucination cleanup of 3-7 rows, rollback of 3-7 rows) while still blocking catastrophic truncation.

**Where to call:** before every `pd.to_csv` and after every `csv.DictWriter` append loop that targets a safeguarded seed. For scripts outside `app/`, adjust `sys.path` to import the helper.

**What NOT to safeguard:** don't add entries for seeds that are routinely rewritten from scratch by the scan pipeline (e.g. `dbt_column_lineage`, `dbt_model_catalog`). The floor pattern only makes sense for demo-critical seeds whose row-count trajectory is monotone or near-monotone.

**When the safeguard raises:** do NOT bypass by wrapping in try/except. A raise means either (a) a writer has a bug, or (b) the rule bounds need widening for a new legitimate operation. Fix the root cause in one of those two places.

Learned from: Session on 2026-04-17. `s2t_mapping.csv` was wiped to header-only mid-session, blocking Deploy and Build-S2T flows. Git-log forensics plus the `s2t_sync_warnings.log` file narrowed the window but could not pinpoint the exact wiping call — the verify harness that was the most plausible culprit had already been deleted. Rather than hunt every pandas roundtrip site across three pages and four scripts, we added `app/_csv_safeguard.py` and wired it into the three known `s2t_mapping` writers (`Business_Glossary.py` Deploy step a + rollback, `scripts/sync_s2t_from_dbt.py` `save_csv`). Any future wiper now raises `RuntimeError` instead of silently corrupting the seed.

**Extension 2026-04-18 — DictWriter truncate-then-fail trap:** The row-count safeguard alone is not sufficient. `csv.DictWriter` opens the file in `"w"` mode (truncating to 0 bytes) BEFORE validating row dict keys against the declared fieldnames. If any row carries an extra key, DictWriter raises `ValueError` mid-write — AFTER the file has been truncated and the header line already flushed. Observed signature: corrupted header-only CSV (205 bytes on a 13-field schema) with no `RuntimeError` trace in logs because the actual exception is `ValueError` from deep inside `csv._dict_to_list`.

Root cause in practice: fieldnames captured once at function entry, then a later loop mutates row dicts to add a new derived key (e.g. `transformation_logic_plain_business`), and the stale fieldnames list never gets refreshed before the write.

Two-part fix added to the safeguard:
1. **At the writer site** — refresh fieldnames right before each write to cover any keys the in-memory rows currently carry:
   ```python
   fieldnames = list(dict.fromkeys([k for r in rows for k in r.keys()]))
   ```
   `dict.fromkeys` preserves insertion order (unlike `set()`), so CSV column order stays stable run-to-run.
2. **At the safeguard boundary** — `assert_fieldnames_cover_rows(fieldnames, rows)` in `app/_csv_safeguard.py`, wired into every `save_csv` helper BEFORE `path.open("w", ...)`. Raises `RuntimeError` with the specific extra-key list and the row index where it was found, so the traceback pinpoints the drifting key name.

**Order of checks inside save_csv:** row-count first (cheap, catches the most common corruption mode — empty or drastically-shrunk writes), fieldnames second (catches schema drift). Both run before the file is opened for writing, so the file stays intact on any safeguard raise.

**General principle:** any "open for write then validate inputs" pattern has this trap. Move all input validation BEFORE `file.open("w", ...)`. For seeds with evolving schemas (downstream LLM enrichment adds derived columns), refresh fieldnames at each write site rather than capturing once at function entry.

## RULE 38: LLM-generated SQL gets 3 retry attempts with error feedback
When LLM-generated SQL fails `dbt run` with a parseable schema error (Binder Error: column not found, table not found), the system feeds the error + candidates + actual schema back to the LLM for correction. Max 3 attempts total. After that, rollback + direct user to Guided Analysis for deeper term context.

**Error feedback shape — the LLM needs all three, not two:**
1. The dbt error message verbatim (so the LLM sees the full engine diagnostic — file, line, column, table).
2. DuckDB's `Candidate bindings:` suggestion (the engine's closest-match hint).
3. `information_schema.columns` dump of every table the failed model `ref()`s (ground truth — the engine's hint may not contain the right column).

Without schema context, the LLM guesses again from the same priors that produced the wrong name in the first place — same class of error, just a different plausible hallucination. With schema context, the LLM usually picks the right candidate in attempt 2.

**Non-retryable errors:** syntax errors, subprocess timeouts, permission/lock errors. These don't benefit from LLM repair — fall through to rollback on the first failure. Only parseable Binder column-not-found / table-not-found errors enter the retry loop.

**File-overwrite discipline:** Each retry overwrites the `.sql` file on disk with the LLM's corrected version. The rollback handler (unchanged) deletes all files in `written_files` on final failure, so an overwritten-then-rolled-back file is still cleaned up — no orphan `.sql` files left in `dbt/models/`.

**Token budget:** Each repair prompt costs ~800-1500 tokens (much shorter than the initial `create_s2t_with_implementation` call that generated the SQL). Worst case 3 retries = ~4500 tokens. Cheaper than a full Guided Analysis run for adjacent-column hallucinations.

**General principle:** any LLM → SQL → execution loop should pass failures back with the error + engine hint + actual schema. Errors are the cheapest teacher.

Learned from: Session on 2026-04-18. `cpe_active_deployed_count` Deploy repeatedly failed at `dbt run` with `Referenced column "material_description" not found. Candidate bindings: "material_number", "model_description", "days_since_receipt", "manufacturer", "serial_number"` — a textbook adjacent-column hallucination where the LLM wrote `material_description` but the actual column is `model_description`. V1 isolation test confirmed: parser extracts all three pieces correctly; schema dump resolves `{{ ref('dim_equipment') }}` to `main_marts.dim_equipment` with the full column list; live `repair_dbt_model_sql` call returned SQL with `material_description` replaced by `model_description` on first retry. Retry loop capped at 3 so a pathological LLM that keeps hallucinating eventually falls through to Guided Analysis rather than burning tokens forever.

## RULE 39: Rollback by ID, not by position
When rolling back an append operation that may be followed by other mid-pipeline mutations (Rule 14 enforcement, deduplication, LLM-generated row insertions, placeholder reshuffling, etc.), rollback must identify rows by **tracked IDs**, not by position (`iloc[:-N]`). Position-based rollback assumes the append was the last write to the table; any downstream writer violates that assumption silently.

**Pattern:**
```python
# Append phase — track IDs as you write:
written_ids = []
for row in new_rows:
    writer.writerow(row)
    written_ids.append(row['id'])

# Rollback phase — match by ID:
df = pd.read_csv(path)
df = df[~df['id'].astype(str).isin(written_ids)]
df.to_csv(path, index=False, lineterminator='\n')
```

**Side benefit:** if downstream processing SWAPS or MODIFIES one of the tracked rows' IDs (e.g. Rule 14 re-ID'ing an orphan), the rollback still removes the original tracked ID cleanly. It may leave the swapped replacement, but that's a strict improvement over the brittle `iloc[:-N]` behavior which could delete entirely unrelated rows.

**Applies to any write path followed by downstream processing that may modify the same table.** In this project: the Deploy `_rollback()` closure in `Business_Glossary.py` — Step a appends S0xx rows, the full end-of-task pipeline may run Rule 14 in `sync_s2t_from_dbt.py` which inserts placeholder rows between Step a and any eventual failure, and only then does rollback fire. Position-based rollback would remove Rule 14's placeholders instead of Step a's additions, leaving the CSV in an inconsistent state (confirmed on 09:02 Deploy — CSV ended at 36 rows instead of the expected 33).

**What not to do:** don't try to "freeze" the CSV between Step a and rollback to prevent downstream writes. The downstream writes are legitimate and need to happen. Track IDs and let rollback be a targeted surgical delete.

Learned from: Session on 2026-04-18. The deploy auto-retry triggered Rule 14 placeholder insertions between Step a append and the end-of-task rollback. `iloc[:-4]` rollback deleted 4 Rule 14 placeholders, leaving Step a rows untouched and CSV in a 36-row inconsistent state. V3 simulation confirmed the new ID-based rollback correctly removes only tracked Step a IDs even with 2 orphans dropped and 5 placeholders inserted between append and rollback.

**Extension 2026-04-18 #2 — rollback must also clean downstream artifacts scoped to the current term:** The ID-based rollback above correctly removes the primary append, but downstream processing (here: Rule 14 inside `sync_s2t_from_dbt.py`) can create *secondary* rows that piggy-back on the same resources the rollback is unwinding. In this project Rule 14 inserts placeholder s2t_mapping rows that reference the `.sql` files the rollback is about to delete. Without the extension, those placeholder rows survive rollback and point at non-existent models — not fatal (next scan flags + cleans them) but it leaves the CSV in an inconsistent "failed Deploy" state between attempts.

Fix: in addition to `~df['id'].isin(tracked_ids)`, also drop rows matching `df['business_term_id'] == current_term_id AND df['target_model'] IN deleted_model_names`, where `deleted_model_names` is computed from the `.sql` files the rollback is deleting. **Scope the downstream drop to the current term_id** — this prevents touching rows belonging to a different term that happens to reference the same `target_model` name (e.g. a previously-deployed term that shares a model with today's Deploy). The term-id proxy is preferred over a `was_new` flag because it handles re-Deploy of an existing term correctly without extra tracking state.

Read the CSV for this filter with `keep_default_na=False, dtype=str` so the string comparisons see raw values, not parsed NaN. Use `assert_csv_safe` on the post-filter DataFrame before writing back — the row-count safeguard catches any filter that would over-remove.

V1/V2 isolation simulation confirmed the proxy holds: unrelated BG999 rows on the same `target_model` survive a BG026 rollback.

## RULE 40: Semantic validation before Deploy finalizes
SQL that compiles and runs is not proof it measures what the business term claims to measure. The deploy auto-retry fixes parseable column-not-found errors. The semantic validation gate adds a second gate between `dbt run` success and `dbt test`: an LLM validator checks the model against the term across three **deterministic** dimensions:

- **Grain** — does the row count / granularity match `term.grain`? "per plant" means one row per plant; "per serial number" means one row per serial; a single-row aggregate is valid only for terms whose `term_name` starts with `total_` / `sum_` / `avg_` / `count_` etc.
- **Filter** — do the exclusions in `term.definition` and `term.notes` appear as WHERE / NOT EXISTS / anti-join conditions in the SQL?
- **Unit** — does the primary output column type match `term.unit`? `count → BIGINT/INTEGER`, `percent → DECIMAL 0-1 or 0-100`, `days → integer or decimal days`, `currency → DECIMAL`.

**Definition drift intentionally NOT checked.** Subjective, overlaps with the three above, produces false positives. Can be added later if real mismatches slip through.

**Be conservative.** The validator should flag `severity=critical` only on HIGH-confidence mismatch — false positive (blocking a legitimate Deploy) is much worse than false negative here. Ambiguous cases → `severity=warning` (informational, does not block). The gate is explicit: `match = (no critical issues)`.

**Repair shape:** when critical issues fire, `repair_semantic_mismatch(term_row, model_sql, issues)` returns a corrected SQL that preserves jinja `{{ ref(...) }}` calls and the CTE/join/projection structure. Only the specific WHERE clause, GROUP BY, or CAST needed to address each critical issue changes. Max 3 total attempts. After 3 fails → rollback with message pointing to Guided Analysis.

**Two fail modes feed the same rollback:**
1. `SemanticValidationFailed` (exhausted retries) — model still mismatches after 3 repair attempts. User message: "Deploy aborted: generated model semantically mismatched term definition after 3 repair attempts. Issues: {critical}. Run Guided Analysis to give the LLM deeper domain context, then retry."
2. `SemanticRepairProducedBrokenSQL` — a repaired SQL no longer compiles. User message: "Semantic repair produced broken SQL. This usually means the term definition is ambiguous or conflicts with available source data. Review term definition and retry."

**Token budget:** happy path ~1500 tokens; worst case 3 validate + 2 repair ≈ 8000 tokens. Cheaper than one Guided Analysis cycle.

**Validator inputs:** `{term_row, model_name, model_sql, row_count, column_types, sample_rows[:5]}` — everything a human reviewer would look at: the term's claim, the actual view's shape, and the SQL that produced it. `sample_rows` is trimmed to 5 rows to keep tokens in check.

Learned from: Session 2026-04-18. The archive of `cpe_active_deployed_count` happened because its deployed SQL was a company-level single-row aggregate while `term.grain` was "per serial number" — dbt ran green, tests passed, Deploy claimed success, but the dashboard number was measuring the wrong thing. V3 live-API test fed the exact same term + compiled SQL into the new validator; it independently flagged `critical grain` mismatch with summary "term expects per-serial-number detail rows but model provides single aggregate row". Gate would have blocked that original Deploy, forcing a repair or Guided-Analysis cycle before the mismatch shipped.

## RULE 41: Deploy handler must reject archived terms at the button
Streamlit button-widget state keyed by `f"deploy_models_{term_id}"` or `f"create_s2t_{term_id}"` can persist stale term references after a term has been archived mid-session. Without an explicit guard at the top of the click handler, the button can fire against an archived term — writing phantom s2t rows against a `target_model` whose `.sql` has already been moved to `dbt/models/archive/<ARC-...>/`. Observed downstream symptoms: scanner emits no lineage (no source `.sql` in non-archive folders); UI renders "No column lineage recorded" and "no SQL captured" for every target column; DuckDB view lookup returns `CatalogException` on every projected column; Deploy itself reports SUCCESS because it only wrote s2t rows and ran Step c/f without Step b/d/e — no functional model was ever built.

**Guard pattern** (paste as the first thing inside each `if st.button(...):` block):
```python
if str(term.get('status', '')).strip().lower() == 'archived':
    st.error(
        f"Cannot deploy archived term '{term.get('term_name', '')}'. "
        f"This term was archived on {term.get('archived_at_utc', '') or 'unknown date'}. "
        "Select an active term or create a fresh one via the New Term form."
    )
    return
```

**Apply to BOTH the Deploy AND the Create S2T button** — no point spending LLM tokens on an archived term either. Put the guard BEFORE any `st.spinner(...)`, BEFORE any subprocess invocation, BEFORE any CSV write. The `return` exits the enclosing `render_ask_claude` function cleanly; the page stays rendered so the user can see the error and re-select.

**Why `status`-at-read-time is the right check** (not widget-key validation): Streamlit reruns the script top-to-bottom on every interaction, including the button press. By the time the click handler runs, `term.status` is the freshest value from the glossary query. The guard is cheap (single string comparison), placed at the point of action (button click), and is robust to any upstream selector-filtering bug that lets an archived term appear in the UI. Belt-and-suspenders with the `_active_glossary` post-filter from hotfix 6.

**Non-reachable cases (still guarded for safety):** if the user manually constructs a URL / uses the st.selectbox keyboard tab order to land on a term mid-archive-flow, or if a widget-state collision promotes an archived button-widget press, the guard still fires before any destructive action.

Learned from: Session 2026-04-18. Deploy fired three separate times against the archived BG026 (09:02, 10:57, 13:17 — each after a session refresh), writing a cumulative 6 phantom s2t_mapping rows pointing at `target_model=obt_active_cpe_count` whose `.sql` lived only in `dbt/models/archive/ARC-20260418-001/obt/`. Resolved by adding the guard to both button handlers; `known_issues #16` flipped to resolved.

## RULE 42: Lookup scope must match selector scope — project-wide
Filtering a selector's options list is only half of an archive-aware UI. The **post-selection lookup** that turns the user's chosen `display_name` back into a full row MUST scope to the same filter as the list. Otherwise — when two glossary rows share a `display_name` after archive-then-recreate-same-name — a naive `glossary[glossary['display_name'] == X].iloc[0]` against `ORDER BY id` data picks the OLDER archived row, not the drafted active one the user is seeing in the selector.

**Symptom to watch for:** user reports "my new term still shows as archived". Tabs look wrong even though the selector only offers one item. The selector isn't lying; the lookup scope is.

**Pattern (centralised helper at module level):**
```python
_active_glossary = glossary[glossary.get('status', 'active').astype(str) != 'archived']

def _resolve_active_term(selected_name: str):
    matching = _active_glossary[_active_glossary['display_name'] == selected_name]
    if matching.empty:
        st.error(f"No active term matches '{selected_name}'. Refresh the page.")
        st.stop()
    if len(matching) > 1:
        matching = matching.sort_values('created_date', ascending=False)
    return matching.iloc[0]
```

Every selector-lookup site (Term Detail, S2T Specification, Data Quality, plus any future tab) calls `_resolve_active_term(selected_name)` instead of `glossary[...].iloc[0]`. Centralising the resolution means future selectors get correct behavior by default.

**Don't confuse three different scopes:**
- **Selector list** (what user sees): `_active_glossary` — archived hidden.
- **Post-selection lookup** (what UI renders): `_active_glossary` — same as selector. THIS RULE.
- **LLM prompt context** (what Claude sees): usually `_active_glossary` — archived terms leak stale definitions and "this already exists" confusion. Treat as a third scope that usually mirrors the selector.
- **Audit lookup by explicit id** (e.g. `metric_card` tooltip, Fix B analysis_findings inheritance by term_name): full `glossary` — legitimate audit path, archived rows needed for historical display.

Classify each grep hit before changing it — converting an audit lookup to `_active_glossary` would break tooltips.

**Project-wide audit trigger (Revision 2026-04-18):** this rule applies to EVERY selector-lookup pair in the app, not a single page. Any significant refactor of a selector or the glossary schema must re-grep `glossary[glossary[` / `glossary.loc[` across `app/` and reclassify every hit. Scope drift is silent — the page renders, just against the wrong row.

**Revision 2026-04-18 #5 — Fix B removed entirely.** Previous revisions of this rule distinguished "audit lookup by term_name" (Fix B: resolve `current_term_id → term_name → set of matching ids → isin()`) as a LEGITIMATE exception for `analysis_findings` inheritance across archive/re-create cycles. That exception is now withdrawn. **All `analysis_findings` lookups use naive `business_term_id == term_id` match.** Archive is final: findings stay with the archived `term_id` as audit trail; a re-created same-named term starts with zero findings and must run fresh Guided Analysis. Cross-term data bleeding was causing more bugs than it solved (hotfix 8 caught the first, hotfix 8 extension caught the second, a third surfaced as analyst confusion about which term "owned" a profiling run). Clean per-term-id semantic — matching the way `s2t_mapping` and `domain_facts` already behave — wins over the convenience of inheriting LLM output. See decision #67 `fix_b_reverted_archive_is_final`.

**Scope table per data type (post-revision #5):**
- Selector list: `_active_glossary`.
- Post-selection lookup: `_active_glossary` (via `_resolve_active_term`).
- LLM prompt context: `_active_glossary` (archived leaks stale definitions).
- `analysis_findings` / `s2t_mapping` / `domain_facts` display: **naive `business_term_id == term_id` — no term_name resolution.**
- Audit lookup by explicit id (e.g. `metric_card` tooltip): full `glossary` — legitimate audit path.

Learned from: Session 2026-04-18 (archive-workflow hotfix 8 and its two extensions). Fix A (hotfix 6) filtered the Data Analysis selector; three Business Glossary tabs (Term Detail L2191, S2T Specification L2345, Data Quality L3123) still did post-selection `glossary[...].iloc[0]` on the full glossary. After user archived BG026 and created fresh BG027 with the same `display_name`, every tab resolved the click to the archived BG026 (hotfix 8 fixed these via `_resolve_active_term`). Then the `_load_term_analysis_findings` guard at L1095 was missed and the S2T Specification tab blocked Create S2T for BG027 despite Data Analysis showing 5 inherited findings — user interpreted the tab disagreement as a sign that the whole inheritance model was causing more confusion than value. Fix B was reverted in hotfix 8 extension 2 (this revision).

## RULE 43: Facts carry hash keys + natural keys, not other dim attributes (ergonomic Kimball)
This project deliberately deviates from strict Kimball. Facts in `dbt/models/marts/fact_*.sql` carry BOTH the surrogate-style hash key (e.g. `hk_material`) AND the corresponding natural key from the source (e.g. `material_number`). Strict Kimball would carry only the hash key and require a join to `dim_material` for the natural key.

**Why we carry both:**
- Hash keys here are deterministic (`hk_X = MD5(natural_key)`), not opaque integers — the two columns are 1:1 mapped, so the redundancy is cheap (no information added, just access path)
- Analysts running ad-hoc SQL can filter / group by natural keys (MATNR, LIFNR, EBELN) without joining to dims
- Star joins still work via hash keys for performance-sensitive paths
- The pattern is conservative — facts carry only the natural keys that match a hash key, NOT other dim attributes

**What facts must NOT carry:** other dim attributes — `vendor_name`, `material_description`, `plant_city`, `equipment_category`, etc. Those stay in dims (or get surfaced via OBT for dashboard ergonomics). Carrying dim attributes in facts is the bad kind of denormalization (drift risk if the dim updates, storage bloat, two sources of truth for human-readable data).

**Allowed pairs (current convention):**
- `hk_material` + `material_number`
- `hk_vendor` + `vendor_id`
- `hk_plant` + `plant_code`
- `hk_equipment` + `equipment_number`
- `hk_purchase_order` + `purchase_order_number`

**Audit pattern:** for each fact, the only non-measure columns should be hash keys + matching natural keys + a small number of degenerate dimensions (date truncations like `month`, `quarter`, status flags like `delivery_status`). If a fact carries `vendor_name` or `material_description`, that's a violation — push it back to the dim and consume via OBT or analyst-side join.

**Drift risk note:** the natural-key value in a fact could become stale if the source re-keys (rare in SAP — MATNR, LIFNR, EBELN are stable). Production rollouts on real data should monitor with a periodic `fact.NK = dim.NK` consistency test. Synthetic demo data has no drift risk.

**Aggregate facts are exempt from the pair requirement:** facts that GROUP BY natural keys (e.g. `fact_goods_receipts_monthly` aggregates by month, `fact_goods_receipt_accuracy` aggregates by vendor × quarter) carry neither hash keys nor natural keys at the aggregated grain — they hold measures + the grouping dimensions that DEFINE the grain. Adding `hk_vendor` to `fact_goods_receipt_accuracy` would be wrong because the row is "all receipts for this vendor in this quarter," not "this vendor."

Learned from: Session 2026-05-05 — user spotted `fact_inventory` carrying both `hk_material` and `material_number` and asked "is this Kimball?" Audit found 5 of 9 facts follow this convention. Codified rather than refactored: the redundancy is deliberate and analyst-facing.
