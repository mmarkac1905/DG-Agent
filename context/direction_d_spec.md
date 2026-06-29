# Direction D — Empirical join cardinality as Stage A evidence (v3)

**Status:** spec of record, draft 3 (final — ready to commit as `context/direction_d_spec.md`).
**Captured:** 2026-04-24
**Supersedes:** Direction D draft 2 (same session, 502 LOC). v3 integrates 4 additional flags (16–19) from trace round 4 and tightens convergence-risk framing. Trace investigation is concluded at diminishing-returns checkpoint after 4 rounds / 19 flags.
**Parallel to:** Direction C (deferred archive/re-run work, committed as e397c78).
**Scope reference:** known_issue #85 (grain analyzer empirical cardinality gap).
**Theme 1 positioning:** deferred. Theme 1 scoping investigation pending; separate follow-up will append positioning once retrieved.

---

## 1. Problem statement

BG027's Deploy produced a 304M-row cartesian explosion because Create S2T chose MATNR as the equi↔mseg join key. MATNR is shared across every scope table at 100% referential integrity — it looks like the canonical key by every static signal the pipeline produces today. It is in fact a classification code shared across thousands of equipment units, fanning out an average of 968,000 rows per equipment record when joined to mseg directly.

Investigation across six ground-truth passes this session established that the failure was not a Create S2T bug. Every layer performed as designed. Integrity is "every source value exists in target"; it measures presence, not selectivity. Nothing in the stack empirically measures what happens when you actually join tables on a candidate key.

The symmetric framing of the workflow clarifies where the fix belongs: today Stage A can tell the analyst "you need these 5 tables and on 2 of them EDA hasn't run yet." It should be equally capable of telling the analyst "you need these 5 tables, but no per-record join key exists between equi and mseg — add SER03, SERI, SER01 from your ingestion layer, those provide the serial-number bridge this term actually requires."

SER03, SERI, and SER01 are already in `raw_sap` (2,157 / 8,777 / 2,200 rows respectively). They are **ingested but not scoped**. Direction D addresses this case — the "scoped-missing" case. The parallel case of "genuinely un-ingested" tables (where a term needs a table that is not in raw_sap at all) is out of scope for Direction D and deferred to a later phase. See §8 for the Path A/B decision rationale.

## 2. Integration constraints (ground truth from trace investigations)

Nineteen flags across four trace rounds established the following ground truth. Direction D's integration points are constrained accordingly. Trace investigation was concluded at diminishing-returns checkpoint after round 4 (flags 16–19 added minor refinements; subsequent rounds would produce noise without reshaping design).

### 2.1 Existing infrastructure state

**F1. The scope-the-term UI is an inline `with tab_scope:` block** at `app/pages/Data_Analysis.py:1182-1287`, not a handler function. Direction D integration points describe edits to this inlined block; no extraction refactor is prerequisite.

**F2. "Missing EDA" is two independent mechanisms.**
- Pre-confirmation: LLM emits `missing_domain_eda` blockers, validated structurally but not cross-checked against actual DAR counts (F11 below).
- Post-confirmation: rule-based DAR count in `check_prerequisites`, parse-naive on `source_tables` (F10 below).

Direction D's cardinality signal attaches to **both** — LLM-time evidence (primary) and rule-time prereq check (safety net). v1's treatment of them as equivalent partners was wrong; v2+ positions them as different-integrity gates at different lifecycle points.

**F7. Blockers are advisory, not gating.** Today, a proposal with unresolved `missing_table` blockers can be confirmed. Only `validation_issues` entries hard-block confirmation (`_render_proposal_section:1128-1130`). Direction D's cardinality-triggered refusal elevates to `validation_issues` to achieve hard-gate behavior — see §2.2.

**F8. `confirm_scope`'s raw_sap check is dead code under UI flow.** The validator (`_scope_derivation.py:520-526`) catches non-raw_sap tables and routes them through `validation_issues`, which the UI already blocks. `confirm_scope`'s line 877-885 check only fires if the UI is bypassed (tests, scripts, manually edited history JSON). Direction D enforces via `validation_issues`, not `confirm_scope`.

**F9. The "Add table" dropdown is the only symmetric-correction affordance today.** Analyst can add any ingested table the LLM missed; there is no affordance for "term needs a non-ingested table." Direction D does not add a new affordance; it routes through the existing revise loop, where the LLM incorporates cardinality evidence and proposes adding already-ingested tables (SER03/SERI/SER01) that it previously omitted.

**F4. `ingestion_required` routing is decorative.** The enum value exists (`_scope_derivation.py:548`) and gets a badge in the UI (`Data_Analysis.py:1036`), but no code path consumes it. Direction D does not rely on `ingestion_required` routing to do any work; the badge remains a user-facing label, nothing more.

**F5. `missing_table` has been emitted zero times across 28 terms.** Not LLM shyness — structural impossibility (F12 below). v2+ redefines its semantics for the Path B case.

**F16. Case C vs Case D lifecycle distinction.** Today's Stage A produces two separate surfaces at different lifecycle moments:
- **Case C** (draft + proposal history): blocker list rendered pre-confirmation. LLM-authored, can prevent bad confirm if promoted to `validation_issues`.
- **Case D** (post-confirmation): prereq grid with `✅ ready` / `⚠️ needed`. Rule-authored, can only warn after confirm has already happened.

The user's workflow quote — "it will say you need 5 tables, on 2 EDA hasn't run" — referenced Case D's visual pattern, but the *semantics* of "prevent bad confirm" belong to Case C. **Direction D's hard-gate fires at Case C.** Case D's prereq grid gets the cardinality-coverage extension (§7) as a safety net / post-hoc diagnostic, not as the primary gate. The two surfaces have different integrity guarantees and Direction D treats them as such.

**F17. No UI affordance for "I need a non-ingested table."** Revise mode has three dropdowns (add / remove / explain) plus free-text. All dropdowns draw from `_live_raw_sap_tables(conn)`; free-text routes to the LLM which is itself constrained to the ingested catalog (F12). Zero path today — UI or prompt — for the analyst to signal a genuinely-missing-from-ingestion table. **Direction D does not build this affordance** (Path B, §8). Un-ingested case routes through `scope_concern` narrative authored by the LLM when it recognizes the gap; no new control needed.

**F18. Confirm-time side-effect chain is all-or-nothing.** Backup → CSV write → `dbt seed --full-refresh` → `export_parquet.py` → `close_connection()`. Any failure in steps 3–5 restores from `.bak`. **Direction D's hard-gate must fire pre-chain (at validator / pre-confirm), not mid-chain or post-chain.** Injecting a post-chain check corrupts rollback semantics: if the post-chain gate fires, the chain's side effects are already committed, but the .bak protocol assumes failure-triggered rollback of in-progress writes. §6.5 validator is the correct integration point by construction; v3 rules out any alternative placement that would require extending the rollback protocol.

**F19. Blockers are per-iteration snapshots with no carry-forward state.** If the LLM emits a `missing_table` blocker in iter 1 and drops it in iter 2, the analyst sees no signal in the Case C render (`_render_proposal_section:992-995` shows only `_latest_iter`'s blockers). No `resolved` / `acknowledged` / `still_open` state exists. **Direction D handles this via validator re-check, not blocker carry-forward.** §6.5 re-runs cardinality validation on every iteration regardless of blocker state; if the cardinality condition still holds (equi↔mseg still has no viable key, LLM didn't add SER03/SERI), the hard-gate re-fires even if the LLM silently dropped the `missing_table` blocker. Blocker carry-forward lifecycle remains out of scope — validator re-check delivers equivalent safety without touching it.

### 2.2 Constraints Direction D imposes

**Hard-gate semantics.** Cardinality-triggered refusal must elevate to `validation_issues`, not just emit a blocker. Rationale: user's workflow framing ("it will say not possible") is a hard refusal, not a warning. Existing advisory blocker pattern (`missing_domain_eda`) is insufficient. v1 treated this ambiguously; v2+ commits to hard gate.

**No new enforcement channel.** Hard-gate enforcement lives in `validation_issues` (existing), not in a new `feasibility_verdict` field or new confirm-time check. Consistent with F8 (existing validation surface is where enforcement lives) and F18 (pre-chain placement is required for rollback-protocol integrity).

**Per-iteration validator re-check.** Every revise iteration re-runs cardinality validation independent of blocker state (F19 resolution). If the diagnostic condition still holds, the hard-gate re-fires; the LLM cannot silently drop the signal between iterations.

**Case C hard-gate, Case D safety-net.** Direction D's primary enforcement is at Case C (pre-confirm validator, §6.5). Case D (post-confirm prereq grid) gets cardinality coverage as a post-hoc diagnostic (§7), not as the primary gate. F16 constraint satisfied by design.

**No catalog expansion.** `sap_data_dictionary` remains the set of ingested tables. LLM vocabulary stays constrained to ingested tables. Path B of the F12 resolution (see §8).

### 2.3 Fixes Direction D absorbs as prerequisites

**F10. `check_prerequisites` DAR query parse-naive on multi-table `source_tables`.** Query uses exact string equality, so a DAR with `source_tables='ekbe,ekpo'` doesn't count toward either table. 9% of current DARs are multi-table. Direction D's §5 prereq upgrade cannot reuse this pattern. Fix scope: rewrite the query to handle comma-delimited `source_tables` correctly (LIKE with comma boundaries, or a helper that splits + normalizes). ~15 LOC change in `_scope_derivation.py:1099-1103`.

**F11. LLM emits `missing_domain_eda` without DAR-coverage evidence in prompt.** The user prompt has no "DARs per table" block; the LLM infers coverage from catalog sparseness. Direction D adds a DAR-coverage context block to the prompt (primarily for cardinality evidence, but incidentally fixes F11). The LLM gets ground truth about what's been analyzed.

**F13. `missing_table` directive has two incompatible wordings** in `scope_derivation_prompt.md`:
- `:90` — "concept requires a table not in raw_sap. Should be rare." (narrow, structurally un-emittable per F12)
- `:148` — "If a concept can't be mapped, emit a missing_table blocker instead of fabricating." (broad, hand-wavy)

Direction D deletes both and replaces with a single unambiguous wording under Path B semantics (§4.2).

**F14. Stage A and Stage C disagree on "domain EDA ready."** Stage A says `ready` with any single DAR; Stage C requires 8 analyzer types. Direction D's §5 prereq upgrade adopts Stage C's strict definition for the new cardinality dimension. Direction D does not fix Stage A's permissive domain-EDA check — out of scope, flagged for follow-up.

### 2.4 Flags flagged but not addressed

**F12 (Path B resolution).** LLM cannot propose non-ingested tables because its catalog input is the ingested set. Path A (expand catalog to include non-ingested SAP tables) is deferred. See §8.

**F15.** 0-model staging tables pass all current gates. Cardinality analyzer runs against raw_sap (not staging), so it is not blocked by F15. Flag remains; follow-up work.

## 3. The three changes (unchanged from v1)

1. **New analyzer: `run_join_cardinality_analysis.py`** — produces empirical fanout evidence per candidate key per pair, emitted as `join_cardinality` DARs. ~350 LOC. §3 detail.
2. **Stage A prompt amendment + context block + validator hard-gate** — renders cardinality evidence into the propose-scope context, adds DAR-coverage ground truth (incidentally fixes F11), teaches the LLM to cross-reference `typical_join_keys_json` against cardinality DARs and use `missing_table` blockers under Path B semantics, and elevates cardinality-triggered refusals to `validation_issues` for hard-gate behavior (F7). ~60 LOC prompt + ~80 LOC Python. §4 detail.
3. **Prereq check upgrade** — fixes F10 parse bug as prerequisite, then extends `check_prerequisites` to verify pairwise cardinality coverage (F14 strict definition for the new dimension). ~45 LOC. §5 detail.

Total: ~535 LOC across five files. Plus tests and prompt amendments. Target: 1.5 focused sessions.

Explicitly deferred: grain retrofit (Phase 3), Path A catalog expansion (Phase 4+), F14 Stage A/C reconciliation, F15 staging-model gating, retry classifier empirical backing, `typical_join_keys_json` fanout annotation in Layer A compile.

## 4. Workflow placement

Cardinality analyzer is a **catalog-level concern**, not term-level. Placement rules unchanged from v1:

- Primary trigger: post-ingestion. New/refreshed raw_sap tables cause analyzer runs on affected pairs.
- Secondary trigger: manual via Data Analysis tab.
- No propose-scope-triggered runs. Scope-the-term reads existing DARs; does not trigger analysis.
- Staleness: DARs carry `source_row_counts`; prereq treats as stale if either side shifted >10%.
- Initial population: full C(42,2)=861 pair sweep, ~10 minutes wall-clock.

## 5. Phase 2a — `run_join_cardinality_analysis.py`

### 5.1 Purpose

Emit `domain_analysis_results` rows with `analysis_type='join_cardinality'` characterizing empirical row-multiplication per candidate join key (direct or bridge) per pair of raw_sap tables.

### 5.2 Candidate key enumeration

For each ordered pair (t1, t2) where t1 < t2, enumerate candidates from three unified sources:

- **Source A — Exact-name shared columns** (minus blacklist: MANDT, ERNAM, ERDAT, UZEIT, ERZEIT, LOEKZ). All shared names, not single-pick.
- **Source B — schema_discovery FK hints.** Read existing schema_discovery DARs for t1 and t2. Every FK candidate targeting the other side of the pair is a candidate. `referential_integrity_pct` recorded alongside the fanout measurement.
- **Source C — semantic_model role hints.** Every column with `role='key'` in both tables (may dedupe against Source A).

**Bridge candidates.** For each pair, enumerate 2-hop paths: for every third table t3 in raw_sap, if t3 shares a candidate key with both t1 and t2, the path t1→t3→t2 is a bridge candidate. 2-hop only; 3-hop deferred.

Dedup key: `(t1, t2, key_columns, kind=direct|bridge, bridge_via)`.

### 5.3 Sampling strategy

For each candidate:
- **Sample distinct keys from smaller side.** 50–500 distinct keys. 50 floor, 500 cap. If smaller side has <50 distinct keys, use all and mark `sample_saturated=true`.
- **Measure fanout on larger side.** `COUNT(*)` of matching rows per sampled key. Emit `avg_fanout`, `max_fanout`, `stddev_fanout`, `matched_keys / sampled_keys`.
- **Bridge fanout.** Sample from t1, join through bridge t3 to t2, measure total per t1 key.

### 5.4 Four-bucket classification

| Bucket | Condition | Meaning for S2T |
|---|---|---|
| `per_record_key` | avg ∈ [0.9, 1.1] AND stddev < 0.5 AND matched/sampled > 0.8 | Safe per-record join. Primary key for same-grain joins. |
| `header_detail` | avg ∈ [1.5, 100] AND stddev/avg < 1.0 | Bounded 1:N. Safe with aggregation-aware query (GROUP BY header). |
| `catastrophic_fanout` | avg > 100 OR stddev > avg | Forbidden as join key. Cartesian risk. |
| `no_signal` | matched/sampled < 0.1 | Structurally present but empty in data (SERNP='HT01' constant; aspirational integrity). |

Boundary cases classify to the more-conservative bucket. Raw numerics carried in DAR for downstream override.

### 5.5 DAR finding schema

Direct candidate:
```json
{
  "t1": "equi",
  "t2": "mseg",
  "kind": "direct",
  "bridge_via": null,
  "key_columns_t1": ["MATNR"],
  "key_columns_t2": ["MATNR"],
  "source": ["shared_name", "schema_discovery_fk", "semantic_model_role"],
  "referential_integrity_pct": 100.0,
  "sample_size": 500,
  "sample_saturated": false,
  "matched_keys": 10,
  "matched_keys_ratio": 0.02,
  "avg_fanout": 968336.9,
  "max_fanout": 7883480,
  "stddev_fanout": 2453851.9,
  "fanout_class": "catastrophic_fanout",
  "source_row_counts": {"equi": 45000, "mseg": 31965},
  "rationale": "MATNR shared across 10 distinct values in equi; each expands to ~968k rows in mseg. Classification code, not per-record key.",
  "schema_version": "a3f4e2b1c0d9",
  "blockers_addressed": []
}
```

Bridge candidate example in §3.5 of v1 (unchanged).

### 5.6 Output contract

BG027 scope (5 tables, 10 pairs, ~5 candidates/pair incl. bridges): ~50 DARs. Full raw_sap (42 tables, 861 pairs): ~4,000 DARs. Storage: ~2MB.

**source_tables format for cardinality DARs.** Write as sorted lex `"t1,t2"` (matching existing multi-table DAR convention). Because F10 is being fixed as a prerequisite (§2.3), the prereq query will correctly parse this at coverage-check time.

### 5.7 Test strategy

1. `per_record_key` on EKKO↔EKKN via EBELN.
2. `header_detail` on EKKO↔EKPO via EBELN.
3. `catastrophic_fanout` on equi↔mseg via MATNR. **BG027 regression guard.**
4. `no_signal` on equi↔mseg via SERNP bridge. **Fixture reality regression guard.**
5. Bridge enumeration finds objk as 2-hop intermediary between equi and mseg.
6. Sample saturation: <50 distinct keys → `sample_saturated=true`.
7. Staleness: `source_row_counts` drift >10% flagged.
8. Deduplication: re-run doesn't produce duplicate DARs (supersedes).

~10 tests, ~300 LOC.

## 6. Stage A prompt amendment + validator hard-gate

### 6.1 New context block — join cardinality evidence

Add `_render_join_cardinality_block(conn, candidate_tables)` to `scripts/_scope_derivation.py`, invoked from `_propose_or_revise` alongside existing six context blocks.

Rendering format:
```
## Join cardinality evidence

For each candidate pair, here is the empirical fanout measured by sampling:

equi ↔ mseg:
  - Direct via MATNR: catastrophic_fanout (avg 968,336x, max 7.9M).
    Classification code shared across equipment; NOT a per-record key.
  - Bridge via objk (EQUNR → SERNR ↔ SERNP): no_signal
    (0 matches in 1000 samples). SERNP is a profile code, not a serial number.
  - No per_record_key or usable bridge found in current scope-candidate tables.

equi ↔ objk:
  - Direct via EQUNR: per_record_key (avg 1.00x, stddev 0.00). Safe join key.
  ...
```

Candidate tables drawn from term's candidate pool. If <5 DARs for pool, include all; if more, rank by heuristic relevance.

### 6.2 New context block — DAR coverage (fixes F11)

Add `_render_dar_coverage_block(conn, candidate_tables)` rendering per-table analyzer coverage so LLM has ground truth for `missing_domain_eda` emission:

```
## DAR coverage (analyzers run per candidate table)

equi:   completeness ✓, dimensions ✓, magnitude ✓, code_tables ✗, dates ✗
mseg:   completeness ✓, dimensions ✓, magnitude ✓, code_tables ✓, dates ✓
...
```

LLM's `missing_domain_eda` emission now has empirical basis rather than catalog-sparseness inference.

### 6.3 Prompt directive — `missing_table` redefinition (Path B)

Delete both current wordings (`:90` and `:148` — F13). Replace with single unambiguous directive:

> **`missing_table`** — emit when the candidate scope cannot support the term's grain because no viable join path exists between required entities, AND you can identify specific **already-ingested** tables that would resolve the gap. The `tables` field must list raw_sap-present tables the analyst should add to scope. Draw on SAP domain knowledge: serial tracking → SER01/SER03/SERI, cost allocation → CO tables if ingested, document flow → VBFA if ingested. When cardinality evidence shows no per_record_key or usable bridge in current scope, this blocker is **expected output**, not exceptional.
>
> If the term genuinely requires a table that is **not** in raw_sap, do not emit `missing_table` — emit a `scope_concern` blocker with note explaining the ingestion gap. (Path A is out of scope for current Direction D.)

Net effect: `missing_table` under Path B semantics means "add this ingested-but-unscoped table." The un-ingested case routes through `scope_concern` with narrative, preserving signal without promising feasibility detection the system can't deliver.

### 6.4 Prompt directive — cardinality cross-reference

New directive after existing `typical_join_keys_json` usage guidance:

> When `typical_join_keys_json` reports a join key at high referential integrity, always verify the cardinality classification from `join_cardinality` evidence before trusting the key. **Integrity is not selectivity.** A 100%-integrity MATNR FK can still be `catastrophic_fanout` if MATNR is a classification code shared across many records. Prefer `per_record_key` candidates for same-grain joins. Treat `catastrophic_fanout` as forbidden regardless of integrity. Use `header_detail` only when the query aggregates the detail side. When no per_record_key or bridge exists between required entities in the current candidate scope, emit a `missing_table` blocker under the Path B semantics above.

### 6.5 Validator hard-gate (F7 resolution)

Extend `_validate_response` (`_scope_derivation.py:520+`) with cardinality-awareness:

```python
# Additional validation post-LLM
cardinality_dars = _load_cardinality_dars(conn, payload['proposed_tables'])
required_pairs = _enumerate_required_pairs(payload)  # all proposed pairs
for t1, t2 in required_pairs:
    pair_dars = cardinality_dars.get((t1, t2), [])
    viable = [d for d in pair_dars if d['fanout_class'] in
              ('per_record_key', 'header_detail')]
    if not viable and pair_dars:  # have evidence, none viable
        issues.append(
            f"No viable join key between {t1} and {t2}. "
            f"Cardinality evidence shows all candidates are "
            f"catastrophic_fanout or no_signal. "
            f"Propose adding tables that provide a bridge "
            f"(see missing_table blocker in this proposal)."
        )
```

**Hard-gate effect.** `validation_issues` entries already block confirm at `_render_proposal_section:1128-1130`. By appending to `issues`, Direction D elevates cardinality failures to hard-gate status without introducing a new enforcement channel. F7 resolved.

**Pre-chain placement (F18 constraint).** The validator runs before `confirm_scope`'s side-effect chain (backup → seed → parquet → close). This is the only placement compatible with the existing all-or-nothing rollback protocol; a post-chain check would corrupt `.bak` semantics. §6.5 is by construction the correct integration point.

**Per-iteration re-check (F19 resolution).** Cardinality validation runs on **every** revise iteration, independent of blocker state. If the LLM drops a `missing_table` blocker it previously emitted (blockers are per-iteration snapshots, no carry-forward state), the validator re-emits the `validation_issues` entry whenever the underlying cardinality condition still holds. Safety does not depend on blocker lifecycle; it depends on the diagnostic condition, which the validator re-evaluates every time.

**Escape hatch.** If the LLM *has* emitted a `missing_table` blocker proposing resolution for the same pair in the current iteration, the validation_issues entry downgrades to a warning (not hard-gate). This lets the analyst see the revision path without being blocked when the LLM has correctly diagnosed the problem. Pattern: LLM diagnosed → warning → analyst reviews and revises. LLM missed it (or dropped it in revise) → hard gate → must revise before confirm.

```python
if not viable and pair_dars:
    # Check if LLM already proposed resolution
    missing_table_blockers = [b for b in payload.get('blockers', [])
                              if b.get('type') == 'missing_table']
    if missing_table_blockers:
        warnings.append(...)  # soft warning
    else:
        issues.append(...)    # hard gate
```

### 6.6 Python implementation size

- `_render_join_cardinality_block`: ~35 LOC.
- `_render_dar_coverage_block`: ~25 LOC.
- `_load_cardinality_dars` + `_enumerate_required_pairs` helpers: ~20 LOC.
- Validator extension: ~25 LOC.

Total: ~105 LOC in `_scope_derivation.py`. Prompt delta: ~60 LOC in `scope_derivation_prompt.md`.

## 7. Prereq check upgrade

### 7.1 Prerequisite fix — F10 parse bug

Before the cardinality extension, fix `check_prerequisites` DAR query:

**Current** (`_scope_derivation.py:1099-1103`):
```sql
SELECT COUNT(*) FROM main_seeds.domain_analysis_results
WHERE LOWER(source_tables) = LOWER(?)
  AND status = 'success'
```

**Fixed:**
```sql
SELECT COUNT(*) FROM main_seeds.domain_analysis_results
WHERE (
  LOWER(source_tables) = LOWER(?)
  OR LOWER(source_tables) LIKE LOWER(?) || ',%'
  OR LOWER(source_tables) LIKE '%,' || LOWER(?)
  OR LOWER(source_tables) LIKE '%,' || LOWER(?) || ',%'
)
AND status = 'success'
```

Pass the table name four times (exact; prefix; suffix; middle). Alternative: helper function that splits `source_tables` at query time using DuckDB's `string_split`, join back, count. Either is ~10-15 LOC. Pick the simpler reviewer-friendly form.

Test: DAR with `source_tables='ekbe,ekpo'` now counts toward both `ekbe` and `ekpo` prereq checks.

### 7.2 Cardinality coverage extension

Extend `check_prerequisites` output with pairwise status:

```python
pairwise_cardinality_status: dict[tuple[str, str], str]
  # "ready" | "no_coverage" | "no_viable_key" | "stale"
pairwise_next_steps: list[str]
```

For each unordered pair of confirmed-scope tables:
1. Load cardinality DARs with `source_tables = sorted_lex([t1, t2])`.
2. If none → `no_coverage`.
3. Else if none have `fanout_class ∈ {per_record_key, header_detail}` → `no_viable_key`.
4. Else if any viable DAR is stale (`source_row_counts` drift >10%) → `stale`.
5. Else → `ready`.

Next-step generation:
- `no_coverage` → "Run join cardinality analysis on {t1}↔{t2}: no cardinality DARs found."
- `no_viable_key` → "No viable join key between {t1} and {t2}. Review cardinality DARs (DAR-XXXX, DAR-YYYY); scope may need revision — see missing_table blocker history if present."
- `stale` → "Re-run join cardinality on {t1}↔{t2}: row counts shifted >10% since last measurement."

### 7.3 UI rendering

Extend `_render_prereq_section` (`Data_Analysis.py:961-987`) to add third dataframe after "Scope tables & domain EDA status":

```
### Scope pairs & cardinality coverage
| pair       | cardinality_status              |
|------------|----------------------------------|
| equi, mseg | ⚠️ no viable key                |
| equi, objk | ✅ ready (per_record_key via EQUNR) |
| mseg, mkpf | ✅ ready (per_record_key via MBLNR+MJAHR) |
...
```

Pattern mirrors existing domain_eda dataframe. Text-based remediation via `next_steps`. No new widgets, no action buttons.

### 7.4 Stage A/C consistency note (F14)

§7.2's cardinality-coverage definition adopts **strict** semantics: a pair is `ready` only if a viable (non-stale) DAR exists with `fanout_class ∈ {per_record_key, header_detail}`. This matches Stage C's stricter EDA-ready pattern, not Stage A's permissive "any DAR = ready" pattern. The existing domain_eda permissiveness is *unchanged* by Direction D; cardinality is additively strict.

## 8. Path A vs Path B decision (F12 resolution)

### 8.1 Constraint

`sap_data_dictionary` = raw_sap set (42 ≡ 42). LLM's input catalog = ingested tables. LLM cannot name a non-ingested SAP table in a `missing_table` blocker because it has no referent for it. `missing_table` has fired zero times across 28 terms for this structural reason, not LLM shyness.

### 8.2 Path A — expand catalog to include non-ingested SAP tables

Dictionary grows to cover broader SAP surface; each entry gets `ingested=yes/no`. LLM can emit `missing_table` for genuinely un-ingested tables. Delivers full symmetric workflow.

**Cost:**
- Dictionary maintenance burden (what counts as "in SAP"? LoB-specific tables? industry solutions?).
- Catalog rendering change (surface `ingested=no` visibly so LLM understands the distinction).
- Test coverage for "LLM proposes non-ingested table" path, including UI affordance for the analyst response ("do you want to ingest this? or pick a different approach?").
- Risk of LLM hallucinating SAP tables the dictionary claims exist but actually don't.

Out of scope for Direction D.

### 8.3 Path B — current direction

Dictionary remains ingested-set. LLM's `missing_table` semantics redefined (§6.3) to mean "add this ingested-but-unscoped table." Un-ingested case routes through `scope_concern` with narrative.

BG027's actual case is Path B (SER01/SER03/SERI are ingested, just unscoped). Path B resolves BG027 and the broader class of scoped-missing failures without requiring dictionary expansion.

### 8.4 Deferral rationale

Path A is deferred to Phase 4+ and will be its own Direction (likely E). Triggering condition: a term lands where the genuine need is a non-ingested table and `scope_concern` narrative proves insufficient. No such term exists in the current glossary; deferring until empirical pressure warrants the complexity.

### 8.5 What "you don't have that table in ingestion layer" means in v2

User's workflow quote: *"it will say not possible you dont have that table in ingestion layer."*

Under Path B, this phrase resolves to two sub-cases with different UX:

- **Table IS ingested but not scoped** (BG027's real case): Stage A emits `missing_table` blocker + validator elevates to hard-gate via `validation_issues`. UI shows: *"Cannot confirm: no viable join key between equi and mseg. LLM proposes adding SER03, SERI, SER01 (already in raw_sap, not in current scope). Revise to add, or adjust grain."*
- **Table is genuinely un-ingested**: Stage A emits `scope_concern` with narrative. UI shows: *"Term may require serial-tracking tables. SER01/SER03/SERI are in raw_sap — consider adding. If your SAP system uses a different serial-tracking module not currently ingested (e.g., industry-solution table X), contact data engineering."* Does not hard-gate — analyst decides.

Direction D ships the first sub-case as hard-gate. Second sub-case ships as advisory narrative only.

## 9. Demo arc for BG027

### 9.1 Pre-Direction D (current)

1. Analyst selects BG027, clicks Propose Scope.
2. LLM proposes [equi, mseg, mkpf, objk, mard] based on MATNR-at-100%-integrity signal.
3. Validator passes (all 5 in raw_sap).
4. Analyst confirms.
5. Prereqs pass (all 5 have DARs).
6. Create S2T generates MATNR-joined SQL.
7. Deploy → 304M cartesian → OOM → rollback.

### 9.2 Post-Direction D (target)

1. Post-ingestion: `run_join_cardinality_analysis.py` ran across raw_sap. ~4,000 DARs exist.
2. Analyst selects BG027, clicks Propose Scope.
3. Stage A prompt now includes cardinality block (§6.1) + DAR coverage block (§6.2). For every equi/mseg pair, cardinality shows `catastrophic_fanout` on MATNR and `no_signal` on SERNP-bridge.
4. LLM reasons: "Required grain per-equipment. equi↔mseg: no per_record_key in candidate pool. SAP convention for per-equipment movement linkage = SER03 + SERI + EQUI. SER03/SERI are in raw_sap (catalog confirms). Propose adding them. Emit missing_table blocker under Path B: tables=[ser03, seri]."
5. Validator runs. `proposed_tables` = [equi, mseg, mkpf, objk, mard, ser03, seri]. `missing_table` blocker present for ser03/seri. Cardinality validation finds equi↔mseg no-viable-key; since LLM emitted the matching missing_table blocker, downgrade to warning, not hard-gate.
6. UI shows proposal with warning + blocker detail. Analyst reviews, understands, runs Domain EDA + join cardinality on new pairs (equi↔ser03, ser03↔seri, seri↔mseg).
7. Cardinality on new pairs: all `per_record_key`. Analyst returns to proposal, confirms.
8. Prereq check: pairwise cardinality all `ready`.
9. Create S2T generates SER03+SERI bridge SQL.
10. Deploy succeeds.

### 9.3 Alternative branch: LLM doesn't propose SER03/SERI

If §9.2 step 4 fails — LLM sees cardinality evidence but still proposes MATNR-based scope without `missing_table` blocker:

5-alt. Validator runs. equi↔mseg has no viable key (catastrophic_fanout + no_signal). No missing_table blocker. **Hard-gate via validation_issues.**
6-alt. UI shows: "Cannot confirm this proposal. No viable join key between equi and mseg in cardinality evidence. Either revise to add bridge tables (SER03, SERI from raw_sap support serial tracking), or adjust term's grain."
7-alt. Analyst clicks Revise, types "add SER03 and SERI, use serial bridge" as instruction. LLM re-proposes. Back to §9.2 step 5.

**Hard gate prevents the 304M cartesian even if the LLM doesn't converge on the right scope autonomously.** This is the critical safety property v2 buys over v1.

### 9.4 Demo narrative (single paragraph)

"Stage A initially scoped 5 tables based on MATNR-at-100%-integrity signals visible to the compiled semantic model. Direction D's join cardinality analyzer empirically measured that MATNR between equi and mseg fans out 968,000 rows per equipment record. The cardinality evidence, rendered into Stage A's propose-scope prompt, enables two safety properties. First, the LLM sees the fanout and applies SAP domain knowledge — equipment-to-movement linkage routes through serial tracking tables (SER03+SERI), already in raw_sap but unscoped — emitting a `missing_table` blocker under Path B semantics. Second, if the LLM fails to reason correctly, the validator elevates cardinality-incoherent proposals to `validation_issues`, hard-gating confirmation. Analyst revises or negotiates scope with evidence-backed prompts. The catastrophic-cartesian failure mode becomes architecturally unreachable, not just unlikely. ~535 LOC, 15 structural flags addressed, no new enforcement channel introduced."

## 10. Known issues to file

- **#86 — `join_cardinality` analyzer.** Phase 2a implementation. Parent: #85.
- **#87 — F10 `check_prerequisites` parse bug.** Prerequisite fix for §7.1. Separate known_issue so it can be landed independently and closed cleanly.
- **#88 — F11 DAR coverage missing from Stage A prompt.** Incidentally fixed by §6.2; file to close as completed-by-D.
- **#89 — F13 `missing_table` directive contradiction.** Incidentally fixed by §6.3.
- **#90 — F14 Stage A/C domain-EDA definition mismatch.** Not fixed by D. File for follow-up.
- **#91 — F15 zero-model staging tables pass all gates.** Not fixed by D. File for follow-up.

## 11. Deferred / out of scope

- **Grain retrofit** (Phase 3). Reuse candidate enumerator; stop single-picking; emit cardinality evidence even when sum-match fails. Separate regression surface (EKKO↔EKPO must continue to produce valid header_detail findings). Separate spec.
- **Path A catalog expansion** (Phase 4+). See §8.
- **`typical_join_keys_json` fanout annotation** in Layer A compile. Defense-in-depth; not needed given §6.5 hard-gate.
- **Retry classifier empirical backing** (bb67e3a follow-up). Depends on Direction D DARs existing first.
- **F14 Stage A/C reconciliation.** Strict definition for cardinality (D adopts); permissive for domain_eda (D does not touch).
- **F15 staging-model coverage gate.**
- **UI action buttons** (e.g., "Run cardinality now"). Text remediation only, matches existing pattern.
- **3-hop+ bridge enumeration.** 2-hop only in D.
- **Theme 1 positioning.** v3 will append once Theme 1 scoping investigation runs.

## 12. Success criteria

Direction D ships successfully when all hold:

1. `run_join_cardinality_analysis.py` exists, runs on BG027's 5-table scope, emits ≥20 DARs, completes in <60s.
2. equi↔mseg produces `catastrophic_fanout` DAR on MATNR. (Regression §5.7.3.)
3. equi↔objk produces `per_record_key` DAR on EQUNR. (Regression §5.7-adjacent.)
4. Stage A prompt includes cardinality block and DAR coverage block when `propose_scope` runs against BG027.
5. Stage A produces a proposal for BG027 that **either** (a) includes SER03/SERI in `proposed_tables` from first iteration, or (b) emits `missing_table` blocker under Path B semantics proposing them, or (c) validator hard-gates via §6.5 with clear revision prompt. Any of the three is success — the LLM path is preferred, the validator path is the safety net.
6. Prereq check on a scope lacking pairwise cardinality coverage produces `next_steps` entries identifying missing-coverage pairs.
7. F10 fix: DAR with `source_tables='ekbe,ekpo'` correctly counts toward both `ekbe` and `ekpo` in prereq check.
8. No regression in existing propose/revise/confirm flow for BG001–BG026.
9. **Hard-gate property holds:** an attempt to confirm a proposal with cardinality-incoherent joins AND no mitigating `missing_table` blocker is blocked by `validation_issues`, with clear UI messaging.

§12.5 is the demo-critical test. If the LLM, given cardinality evidence + Path B redefinition, still picks MATNR and fails to propose SER03/SERI or emit a missing_table blocker, the prompt amendment needs iteration. Budget: ~1 session of prompt-tuning. Hard-gate (§12.9) always holds regardless, so catastrophic outcomes are prevented even if LLM convergence requires iteration.

## 13. Implementation order

1. **F10 fix** (§7.1). Independent, low-risk, enables everything downstream. ~15 LOC + test. **DONE — commit 8cfe7e4.**
2. **Phase 2a analyzer** (§5). Land + tests. **DONE — commit 14aadd2.**
3. Run analyzer on BG027 fixture; verify §12.1–3. Two fixture defects discovered mid-step (Defect 3 column rename + Defect 1 SERI sparseness); fixed before Step 4 could proceed. **DONE — commit 419d1a9 (fixture prereqs).** Bridge pruning (Amendment 2) added to keep candidate volume manageable. **DONE — commit 520ac5d.**
4. **Context blocks + prompt amendment** (§6.1–4) **+ validator hard-gate** (§6.5). Bundled. Land.
5. Re-run Stage A `propose_scope` on BG027; verify §12.5 (prefer path a/b, accept path c).
6. **Prereq cardinality extension** (§7.2–3). Land.
7. **End-to-end BG027:** propose → cardinality-aware scope → confirm → prereqs ready → Create S2T → Deploy succeeds.
8. **Regression sweep** BG001–BG026.

Each step independently reviewable and committable.

## 14. Standing risks

- **UX convergence risk (not safety risk).** If §6.3 redefinition doesn't trigger the LLM to emit `missing_table` for BG027-class cases on iteration 1, the analyst goes through a revise cycle ("add SER03 and SERI") before the LLM converges. This is a UX cost, not a safety cost: the hard-gate at §6.5 prevents the 304M cartesian regardless of LLM autonomy. Path (c) — validator hard-gate — always holds. The safety property is architectural; LLM convergence is polish. _v3.1 update: budget retained pending Step C (BG027 §12.5 verification); to be dropped if path (a) or (b) succeeds on first test._
- **Bridge enumeration cost.** 42 tables × 2-hop bridges may exceed ~4,000-DAR estimate. Mitigation: obvious-no_signal candidates (key column has <10 distinct values on one side, >10,000 on other) skip sampling. Under 10 min. _v3.1 status: Amendment 2 (commit 520ac5d) reduced BG027 5-table scope from 442 to 192 candidates and 22min→12min wall-clock. Five pairs remain above the per-pair 15-target — flagged for future Direction E (anti-pattern compilation) consideration._
- **Stale DAR proliferation.** Supersedes-chain handles it; no deletion. Archive job separate concern.
- **F10 fix subtlety.** Comma-boundary parsing in SQL is fragile. If review finds the 4-pattern LIKE hard to audit, switch to DuckDB `string_split` helper. Either works. _v3.1 update: F10 fix (commit 8cfe7e4) used DuckDB `list_contains(string_split(...))` form, not the LIKE alternative. Resolved._
- **Path B misdiagnosis.** If a term genuinely needs a non-ingested table and the LLM correctly identifies this but emits `missing_table` (instead of the correct `scope_concern`), the blocker references a non-existent table and the validator rejects the proposal as "proposed_tables not in raw_sap." Analyst sees confusing error. Mitigation: §6.3 prompt directive explicitly warns LLM to use `scope_concern` for un-ingested. Watch for this in first-week testing; if it happens, add validator logic to catch "`missing_table` references table not in raw_sap" and rewrite as `scope_concern` automatically with a warning.

---

## Summary of v3 changes vs v2

- **F16 Case C / Case D distinction explicit** (§2.1, §2.2): hard-gate fires at Case C (pre-confirm), Case D prereq grid is safety-net. User's workflow quote referenced Case D's visual pattern but the gate-prevention semantics belong to Case C.
- **F17 resolution noted** (§2.1): no new UI affordance for un-ingested tables; Path B routes through `scope_concern` narrative.
- **F18 pre-chain placement constraint** (§2.1, §6.5): hard-gate fires at validator, never post-chain; alternative placement corrupts rollback protocol.
- **F19 per-iteration re-check** (§2.2, §6.5): validator re-evaluates cardinality every iteration independent of blocker state; safety does not depend on blocker carry-forward lifecycle.
- **§14 convergence-risk framing tightened**: renamed to "UX convergence risk (not safety risk)"; makes explicit that hard-gate is architectural safety, LLM autonomy is polish.

## Summary of v2 changes vs v1 (retained)

- **Hard-gate commitment** (§6.5): validator elevates cardinality-incoherent proposals to `validation_issues`, making confirmation physically impossible without revision or LLM-proposed resolution.
- **F10 prerequisite** (§7.1): parse bug fix landed first as known_issue #87, unblocks §7.2 cardinality coverage.
- **F11 incidental fix** (§6.2): DAR coverage block added, gives LLM ground truth for all blocker types not just cardinality.
- **F12 Path B scoping** (§8): explicit acknowledgment that LLM can only propose ingested tables; un-ingested case routed through `scope_concern`, Path A deferred.
- **F13 directive cleanup** (§6.3): `missing_table` gets single unambiguous definition.
- **F14 strict semantics** (§7.4): cardinality coverage is strict, domain_eda permissiveness unchanged.
- **F7/F8/F9 clarified** (§2.1): blockers advisory today, `confirm_scope` check dead code, "Add table" dropdown asymmetric — all documented, Direction D routes around rather than fights them.
- **Alternative branch** (§9.3): explicit demo arc for LLM-fails-to-propose case, showing hard-gate delivers safety even when convergence requires iteration.
- **LOC estimate** (~535, up from ~480): F10 fix + validator hard-gate + DAR coverage block are net-new, each small.
- **Known_issues expanded** (§10): six issues instead of one (#86–#91), some closed-by-D, some deferred.

## v3.1 amendments (post-implementation calibration)

**Status:** in-flight; documents the deltas from v3 plan that arose during step 1–4 implementation. v3 body above remains spec-of-record; this section captures what *actually* shipped where it diverged.

### v3.1.1 — Fixture prerequisites landed mid-implementation (commit 419d1a9)

The v3 plan assumed BG027's `equi → seri → mseg` bridge would be discoverable from the raw_sap fixture as-is. Verification probe (step 3 dry-run) revealed two latent fixture defects that blocked any viable bridge from classifying as per_record_key:

- **Defect 3 (column rename):** `SER01.SDAUFNR/POSNR` → `EBELN/EBELP`; `SER03.SDAUFNR/POSNR` → `MBLNR/ZEILE`; `SERI.LIEF_NR/POSNR` → `MBLNR/ZEILE`. SAP-canonical names so Source A (shared-name) bridge enumeration could discover the seri↔mseg leg.
- **Defect 1 (SERI sparseness):** `generate_seri` rewritten from "iterate GR movements opportunistically" (~20% EQUI coverage, ~8.7k SERI rows for 45k EQUI) to "iterate equipment with deterministic round-robin GR pairing" (100% EQUI coverage, 45k SERI rows). 8 affected files + 6+3 seed CSV rows; Croatian descriptions preserved.

Defect 2 (OBKNR namespace harmonization across SER01/SER03/SERI/OBJK) remains **deferred**. No current dbt model joins on cross-table OBKNR. Future Direction (likely E) candidate.

This was not in the original v3 plan. Documented here so future readers don't reproduce the same dry-run discovery.

### v3.1.2 — v3 thresholds VALIDATED; original Amendment 1 REJECTED

A pre-amendment design round proposed widening `per_record_key` from `avg ∈ [0.9, 1.1]` to `[0.9, 1.5]` and relaxing the stddev gate. Calibration probe (`tasks/direction_d_calibration_probe.md`) measured actual distributions on BG027's 5-table scope post-fixture-fix:

- equi↔objk via EQUNR (anchor for per_record_key): avg=1.0000, stddev=0.0000, matched_ratio=1.0 — comfortably inside v3 bounds.
- equi↔mseg via MATNR direct (anchor for catastrophic_fanout): avg=4500, stddev=6488 — comfortably above v3 catastrophic threshold.
- equi↔mseg via seri (EQUNR+MBLNR) (target per_record_key bridge): avg=1.0000, stddev=0.0000, matched_ratio=1.0 — also comfortably inside v3 bounds.

Real per-record cases sit at avg ≈ 1.0 with stddev ≈ 0 in this fixture. The (1.1, 1.5) gap that triggered the original Amendment 1 proposal does not exist in actual data. **Amendment 1 is REJECTED. v3 thresholds ship unchanged.**

### v3.1.3 — Amendment 2 (bridge pruning) landed (commit 520ac5d)

Three rules in `scripts/run_join_cardinality_analysis.py`:

- **Rule 2(a) — t3 role='key' filter.** Both bridge t3 sides (`k3_left`, `k3_right`) must appear in `main_seeds.source_column_roles` with role='key'. t1 and t2 keys are NOT filtered. Direct enumeration is unchanged (still emits shared_name catastrophic_fanout candidates as evidence).
- **Rule 2(b) — type-family compatibility.** `_type_family(data_type)` classifies columns as {string, numeric, temporal, other}. Bridge legs (t1.k1↔t3.k3_left and t3.k3_right↔t2.k2) must match family. Defense-in-depth; rarely fires in SAP fixtures because shared-name columns share types.
- **Rule 2(c) — two-pass short-circuit.** Pass 1 enumerates + measures + emits direct candidates. If any direct classifies as per_record_key, bridge enumeration is skipped entirely for that pair. `header_detail` does NOT short-circuit (a bridge might still find a stricter per_record_key alternative).

**Result:** BG027 5-table scope went from 442 → 192 candidates (−57%); wall-clock 22min → 12min (−40%). Both regression guards preserved (DAR-00027 catastrophic MATNR direct; DAR-00057 per_record_key seri bridge).

**Per-pair budget:** 5 of 10 pairs remain above the advisory 15-candidate target (equi↔mseg=34, mard↔mseg=37, mseg↔objk=52, etc.). All are pairs without direct per_record_key, so the role+type filters cut roughly half the bridges but pairs sharing common keys (especially MATNR) still produce many candidates. The explicit STOP criteria (DAR-00057 preserved, total ≤ 200) both pass; the per-pair targets are informational. Tightening further is deferred — see v3.1.4.

### v3.1.4 — NEW DIRECTION FILED: Direction E (anti-pattern compilation)

The 5 over-target pairs all share a structural pattern: their bridges enumerate many candidates that classify as catastrophic_fanout or no_signal, contributing volume but no signal-gain to the LLM context bundle. Ideas in scope for a future Direction E:

- Compile catastrophic_fanout DARs into a `cardinality_antipatterns` table keyed by `(t1, t2, key_columns)` so the Stage A prompt can render *"the following key combinations are known catastrophic — do not propose"* as a compact summary instead of N individual DARs.
- Skip re-measurement when an antipattern already exists at fresh schema_version (avoid recomputing 968,000-x MATNR fanouts every refresh).
- Provide an explicit "demote bridge enumeration further" rule (e.g., budget cap per pair with priority ordering) once antipattern table exists.

Out of scope for Direction D. File when prioritized; will need its own spec.

### v3.1.5 — Step 4 implementation deltas

Step 4 (this commit) bundles spec §6.1 + §6.2 + §6.3 + §6.4 + §6.5 (validator hard-gate) into a single delivery. The original §13 ordering separated §6.1–4 (step 4) from §6.5 (step 6); the bundling matches the implementation reality and is reflected in the updated §13 above.

The validator's `required_pairs` enumeration uses **`payload['join_path']`** (LLM-declared joins) rather than `C(n,2)` over `proposed_tables`. Per design Round 4 flag B, iterating all proposed pairs over-blocks pairs the term's query never joins. The LLM's `join_path` is part of the §28.11 Stage A contract and is the precise set of joins the term actually requires.

Staleness check (§4 + §7.2 carry-over): a cardinality DAR is treated as stale if `source_row_counts` for either side has shifted >10% since the DAR was emitted. Stale DARs do not satisfy the validator's "viable" requirement.

---

## v3.2 amendments (post-Step-C empirical validation)

**Status:** Direction D propose_scope is COMPLETE. End-to-end verification (confirm + Create S2T + Deploy on BG027) ran in the same closure commit. v3.2 captures the corrections to §12.5 success criteria that Step C's empirical outcome forced.

### v3.2.1 — §12.5 success criteria CORRECTED

The original §12.5 was written from §9.2's pre-investigation prediction that the SAP-canonical equipment-to-movement bridge required SER03 + SERI + EQUI. Empirical reality post-fixture-fix (Defect 3 column rename + Defect 1 SERI sparseness fix):

- **SERI** has SERNR + EQUNR + MBLNR + ZEILE → carries the per-equipment-per-movement linkage. The bridge `equi.EQUNR=seri.EQUNR :: seri.MBLNR=mseg.MBLNR` measures avg=1.0 / stddev=0 / matched_ratio=1.0 (DAR-00328) — clean per_record_key.
- **SER03** has only OBKNR/OBZAE/SDESSION_TYPE/MBLNR/ZEILE/MATNR/MENGE/MEINS — **no SERNR, no EQUNR**. Cannot link equipment to its movements without going through SERI first. SER03 is GR-level serial-document-header, not on the equipment-to-movement path.
- **SER01** is PO-level. Analogous; not on the equipment-to-movement path.

**Corrected §12.5:**

Stage A produces a proposal for BG027 that meets ANY of (multiple satisfactions are fine and expected):

- **(a)** `proposed_tables` includes the table(s) required by viable cardinality bridges for term-required pairs. For BG027 with the per-equipment grain, this is **SERI** (carries EQUNR + MBLNR). NOT SER01 / SER03.
- **(a-bis)** `proposed_tables` includes all tables on the join_path the LLM constructs from cardinality evidence — i.e., the LLM's join_path is internally consistent with its proposed_tables.
- **(b)** `missing_table` blocker — unchanged. Fallback when LLM identifies but cannot resolve the gap.
- **(c)** validator hard-gate — unchanged. Safety net when LLM ignores evidence.
- **(d)** *NEW:* proposed `join_path` uses only candidates classified as `per_record_key` or `header_detail` in cardinality DARs; contains zero `catastrophic_fanout` joins. This captures the OPTIMAL outcome — the LLM didn't just avoid the bad join, it actively chose the best one.

ANY of (a), (a-bis), (b), (c), or (d) constitutes success. The original (a)+(b)+(c)-only formulation was over-specified by the §9.2 prediction; (d) is the canonical "constructive resolution from evidence" outcome we want to reward.

### v3.2.2 — Step C result documented as PASS under corrected §12.5

LLM proposal (verbatim, from `tasks/direction_d_step_c_report.md`):

```json
{
  "proposed_tables": ["equi", "mkpf", "mseg", "objk", "seri"],
  "primary_field_per_table": {
    "equi": "EQUNR", "mkpf": "MBLNR", "mseg": "BWART",
    "objk": "SERNR", "seri": "SERNR"
  },
  "join_path": [
    {"from": "equi",  "to": "objk", "keys": ["EQUNR"]},
    {"from": "objk",  "to": "seri", "keys": ["SERNR"]},
    {"from": "seri",  "to": "mseg", "keys": ["MBLNR", "ZEILE"]},
    {"from": "mseg",  "to": "mkpf", "keys": ["MBLNR"]}
  ],
  "blockers": [
    {"type": "scope_concern", "tables": ["mseg"], "short_title": "BWART movement type semantics unclear"},
    {"type": "missing_domain_eda", "tables": ["equi"], "short_title": "Equipment status mapping needed"}
  ],
  "confidence": "medium",
  "validation_issues": []
}
```

Path verdict under corrected §12.5:
- **(a) PASS** — SERI included in `proposed_tables`.
- **(a-bis) PASS** — every table referenced in `join_path` is in `proposed_tables`.
- **(b) N/A** — no `missing_table` blocker emitted (LLM resolved constructively, blocker not needed).
- **(c) N/A** — validator did not need to hard-gate (no `validation_issues`).
- **(d) PASS** — join_path uses `equi↔objk` direct EQUNR (per_record_key DAR-00332), `seri↔mseg` MBLNR/ZEILE (the per_record_key bridge anchor DAR-00328 manifests as the seri→mseg leg here), `mseg↔mkpf` direct MBLNR (per_record_key DAR-00403). No catastrophic_fanout joins. The MATNR-direct catastrophic candidate (DAR-00298) was visible in the cardinality block and correctly *not* selected.

This is the optimal Direction D outcome — paths (a) + (a-bis) + (d) all fire on first iteration. Constructive resolution from cardinality evidence + SAP knowledge.

### v3.2.3 — Validator coverage gap acknowledged (known_issue #92)

When propose_scope's analyzer scope omits a table that becomes a join_path bridge intermediary or terminus, that pair has no cardinality DARs and the §6.5 validator silently passes — it can't gate on absent evidence. In Step C's case, BG027's analyzer scope was `equi,mseg,mkpf,objk,mard` (5 tables); SERI was used by the LLM as a bridge intermediary but pairs `objk↔seri` and `seri↔mseg` were not in the analyzer's pair-iteration set.

Current mitigation: the LLM still uses cardinality evidence for explicitly-analyzed pairs. The bridge `equi↔mseg` evidence (which DOES exist) drove the LLM to select SERI. The downstream join_path pairs through SERI weren't validated, but the LLM's choice was empirically correct anyway.

Future mitigation (filed as `known_issue #92`): lazy on-demand analysis for join_path pairs without cardinality DARs, triggered at validator time. Severity: low — rarely encountered in practice given direct enumeration covers same-pair candidates and the LLM tends to choose evidence-backed paths. Forward hardening, not blocking.

### v3.2.4 — Direction D status: COMPLETE

The propose_scope path delivers the safety property. End-to-end (propose → confirm → Create S2T → Deploy) validated in this closure commit (see commit message). The catastrophic 304M-row cartesian failure mode is architecturally unreachable on BG027 with cardinality-driven scope.

Remaining items, deferred to separate efforts:

- **Direction E (anti-pattern compilation)** — filed in v3.1.4. Future work to fold catastrophic_fanout DARs into a fast-lookup antipattern table.
- **Spec §13 step 6 (prereq cardinality extension §7.2-3)** — not needed for propose-side correctness. Safety net for analyst review of confirmed scopes; defer.
- **Regression sweep BG001–BG026** — confirms no other terms regress under the new prompt. Schedule as a one-shot batch verification.
- **Path A (catalog expansion to non-ingested SAP tables)** — Direction E or later, per v3.1.

---

## 15. Direction D scope completion

Stage A delivery is **complete** per v3.2 §12.5(a) + (a-bis) + (d). The propose_scope path is safe: cardinality evidence is rendered into the Stage A prompt; the LLM uses it constructively (BG027 Step C: SERI selected as the per-record bridge, MATNR-direct catastrophic correctly avoided); the §6.5 validator hard-gate is in place as the architectural safety net.

End-to-end deterministic quality (propose → confirm → Create S2T → Deploy → finite-row-count) requires **Direction F** (separate spec at `context/direction_f_spec.md`). Step C's e2e probe surfaced the gap: Create S2T runs as a separate LLM call with a separate context bundle (`assemble_context(purpose='create_s2t', ...)`) that does NOT inherit Direction D's cardinality block. With BG027's correct scope_confirmed in hand, Create S2T regenerated the original catastrophic `obj.MATNR = m.MATNR` join, dropping the SERI bridge entirely. The dbt run failed on a separate Binder error (`m.BUDAT` doesn't exist on MSEG); had the SQL been syntactically valid, it would have produced the original 304M-row cartesian.

This is **not a Direction D defect** — Direction D's spec scope is propose_scope, and propose_scope works correctly. It IS a downstream gap in deterministic-quality coverage.

Direction F (filed concurrently) extends the cardinality-evidence + validator-hard-gate pattern from propose_scope to Create S2T. Its three pieces:

1. Pipe `join_cardinality` DARs into the Create S2T context bundle (reuse `_render_join_cardinality_block`).
2. Add a Create S2T system-prompt directive equivalent to §6.4 (treat catastrophic_fanout as forbidden join key regardless of integrity).
3. Post-generation SQL validator: parse the LLM's SQL, look up each JOIN's keys against cardinality DARs, hard-refuse on `catastrophic_fanout`. Trigger lazy on-demand analysis (closes #92) when no DAR exists for the pair.

Direction F's F.3 is the architectural enforcement — equivalent to Direction D §6.5 but at the SQL-output layer instead of the scope-proposal layer. Direction D + Direction F together close the loop from term definition to deployable SQL with no catastrophic path reachable through either LLM call.

This commit lands Direction D Stage A closure (this spec's v3.2 + known_issue #92). Direction F's spec ships separately. Implementation of F.1 + F.2 + F.3 is a future session's work — see `context/direction_f_spec.md`.

---

## Trace investigation close-out

Four trace rounds, 19 flags. Rounds 1–3 materially reshaped the spec (flags 1–15). Round 4 (flags 16–19) added refinements without changing core design. Claude Code identified round 4 as the diminishing-returns checkpoint; further rounds would produce noise without reshaping integration points.

Un-investigated areas explicitly out of scope for Direction D:
- dbt seed shell-out failure modes (unrelated to scope-the-term flow).
- Streamlit session state caching (unrelated).
- Prompt-caching TTL semantics (unrelated).

These are noted for any future Direction that targets them; Direction D does not.
