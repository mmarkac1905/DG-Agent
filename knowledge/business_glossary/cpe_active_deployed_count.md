# Business Term: Active Deployed CPE Count

_Last generated: 2026-07-03 21:52:59_

## Definition

Number of CPE devices currently installed and working at customer locations. Don't count ones still in the warehouse, returned by customer, or sent back to vendor as defective.

- **ID:** `BG027`
- **Owner:** Network Operations
- **Approved by:** Head of Network Operations
- **Status:** `scope_confirmed`
- **Unit:** Count
- **Grain:** per serial number
- **Domain:** inventory
- **Related terms:** [cpe_warehouse_stock_count](cpe_warehouse_stock_count.md) · [cpe_defect_return_rate](cpe_defect_return_rate.md)

**Notes:** Need this for monthly ops review.

## Source-to-Target Mapping

### Source Tables (SAP)

| Table | Field | Description |
| --- | --- | --- |
| EQUI | EQUNR | Equipment master — provides equipment details and installation status for CPE devices. |
| MKPF | MBLNR | Material document header — provides posting dates for goods movements to determine current status. |
| MSEG | BWART | Material movement lines — contains BWART movement types to classify deployment vs warehouse vs return movements. |
| OBJK | SERNR | Equipment object list — bridges equipment to serial numbers for per-serial tracking. |
| SERI | SERNR | Serial number master — provides serial number details and links to goods movement documents. |

### Transformation (plain language)

1. Carries the count of active and deployed CPE devices as a direct pass-through of EQUI.EQUNR from the SAP Equipment master, flowing unchanged through staging, vault, and mart layers.
2. This column carries the material document number from SAP field MKPF.MBLNR, flowing through the staging, vault, and mart layers unchanged to reflect the count of currently active and deployed CPE units based on goods movement postings.
3. Carries the SAP material movement type directly from MSEG.BWART, flowing unchanged through all pipeline layers to classify each movement line as a deployment, warehouse, or return transaction for active CPE unit counting.
4. This column carries the count of actively deployed customer premises equipment and flows through unchanged from SAP field OBJK.SERNR, which bridges equipment records to their serial numbers for individual unit tracking.
5. This column carries the serial number value flowing through unchanged from the SAP field SERI.SERNR, representing the count of active deployed CPE units identified by their serial number records.

### SQL (from dbt models)


## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

Status: `scope_confirmed`

## Related Decisions (20)

- **#58** (2026-04-18) **[NEVER_REPEAT]** — empty_csv_corrupts_duckdb_schema_inference: After any wipe event that resulted in a --full-refresh against a header-only CSV, DO NOT try to re-populate via non-refresh dbt seed. Always run dbt seed --full-refresh --select <seed> once against the restored CSV before resuming normal operations. Consider adding a guard in end_of_task.py CRLF-normaliser loop: if a seed CSV has only a header line (no data rows), skip the subsequent --full-refresh for that seed so the prior valid schema stays in DuckDB. See also decision #57 csv_dictwriter_truncate_trap — this is the downstream collateral symptom.
- **#63** (2026-04-18) **[NEVER_REPEAT]** — semantic_validation_gate_phase14: Semantic validation belongs inside the Deploy flow, not downstream. Compile-time green means nothing for business-correctness. When cost-bounding the validator: only deterministic dimensions that can be checked against declared term attributes (grain, filter, unit). Skip definition drift — it is subjective and overlaps with the other three. Bias toward false negatives (accepting ambiguous cases) since blocking a legitimate Deploy is much worse than letting one through. The live V3 test exposing the cpe_active_deployed_count grain bug validates the choice to ship this gate. See RULE 40.
- **#65** (2026-04-18) **[NEVER_REPEAT REVERTED]** — analysis_inheritance_via_term_name: Preserve historical FKs on writes; resolve by stable natural key on reads. term_name is the stable natural key for a business term across archive/re-create cycles. This pattern avoids schema churn (no foreign-key column swap, no migration) and also preserves audit trail (old BG026 rows stay tagged as BG026 so historical analytics remain correct). Resolution is O(number_of_glossary_rows_with_same_term_name), negligible in practice.
- **#66** (2026-04-18) **[NEVER_REPEAT]** — lookup_scope_matches_selector_scope_projectwide: Selector-lookup scope must match selector-list scope. Any post-selection resolution that feeds UI content must scope to the same filter as the list of options — mismatched scope lets non-listed rows win the lookup. Centralise the resolution in a helper (_resolve_active_term) so future selectors get correct behavior by default. LLM-prompt context is a separate scope question: archive usually wants exclusion (same filter as selector) unless the feature is explicitly about audit. RULE 42 revision emphasises project-wide applicability — audit every selector-lookup pair on any significant refactor.
- **#67** (2026-04-18) **[NEVER_REPEAT]** — fix_b_reverted_archive_is_final: Archive is final — once a term is archived, its analysis_findings stay with the archived term_id as audit trail and do not follow a re-created same-named term. A re-created term starts with zero findings and must run fresh Guided Analysis. This produces a clean per-term-id semantic that matches how s2t_mapping and domain_facts already behave. See RULE 42 revision #5. Cross-term data bleeding was causing more bugs than it solved (decision #66 hotfix 8 caught the first; hotfix 8 extension caught the second; a third surfaced with analyst confusion about which term "owned" a profiling run).
- **#74** (2026-04-19) — empty_scope_new_term_produces_best_effort_model_not_refused: Intentional limitation accepted. The UI should show 'no EDA history for this term — run Completeness/Dimensions first' hint before enabling Create S2T button on brand-new drafts.
- **#76** (2026-04-19) — phase_15a_closed_on_p74_with_honest_acknowledgments: Migration complete. assemble_context() helper + per-source consumption directives are the standing pattern for all LLM calls going forward. Create S2T migration validated. Next work (term-analysis UI, chat) layers on this foundation. Future LLM-call-containing features should default to the directive-per-source-type template instead of rediscovering 'bundle delivery alone is insufficient' (decision #73).
- **#77** (2026-04-19) — ab_test_p75_accepts_partial_quality_evidence_token_efficiency_core_claim: A/B results accepted. Known issues #28 and #29 logged. The term-analysis runner design must include an ontology-layer consumption directive. Migration closure stands — retry loop + gate infrastructure are the validation layer for LLM output quality in production, not regression tests. The A/B results JSON log preserved; token-efficiency comparison table is the empirical evidence. IMPORTANT: this decision does NOT claim NEW path produces higher-quality SQL than OLD path. A/B quality evidence is single-run and partial (4 of 6 quality signals lost to harness bug #28). The defensible claims are: (a) NEW path is 54-79% more token-efficient; (b) NEW path has zero invented citation IDs across 3 runs; (c) NEW path architectural properties (fingerprinting, explicit directives, audit) enable the term-analysis injection loop which OLD path could not support. Quality parity between paths for single-shot Create S2T remains an assumption validated only in production by the deploy auto-retry + semantic gate. If a production regression emerges on NEW path that OLD path would not have shown, re-open this decision.
- **#78** (2026-04-26) — theme_1_c1_dar_status_surfacing_lands_with_loader_saturation_caveat: C1 lands cleanly. Loader + renderer + prompt directive are correct in isolation (proven by 4 unit tests + synthetic real-data smoke). Production value gated on known_issue #93 (LIMIT-50 saturation by join_cardinality). #93 should land BEFORE Theme 1 C2/C3/C4 commits to avoid shipping code that has no production effect. C1 itself is shippable - the renderer is forward-compatible with #93's fix and the unit tests guard against regression in the rendering logic. Recommended sequencing: ship C1 -> fix #93 -> resume C2 (deterministic-analyzer directives) -> C3 (TAR attestation) -> C4 (Stage A blockers).
- **#79** (2026-04-26) — issue_93_filter_join_cardinality_from_generic_dar_dump_option_alpha: #93 closed. C1's STATUS-prefix rendering now reaches the LLM in production. Theme 1 C2 (deterministic-analyzer directives), C3 (TAR attestation), C4 (Stage A blocker surfacing) can now ship with the confidence that their DAR-targeting work has visible production effect. Recommended next: C2 → C3 → C4. Beta (#94) deferred; partition-by-analysis_type (#95) not filed yet (only file if 50/134 EDA coverage proves insufficient for a specific term). Pre-existing weakness noted but not addressed: the dedicated cardinality block does not emit DAR-NNNNN ids verbatim, so cardinality DARs cannot be cited in dar_consumed attestation. Pre-existing condition (was true pre-#93 too); directive #3 doesn't teach cardinality consumption either, so LLM doesn't cite cardinality DAR ids in practice anyway. Future refinement, not a Theme 1 blocker.
- **#80** (2026-04-26) — per_pair_dar_surfacing_replaces_recency_only_limit: Per-pair surfacing is a structural improvement over recency-only - eliminates wasted slots from supersede-bug duplicates, guarantees one row per (type, tables, col_name) partition, and combined with LIMIT 100 fits realistic scope distributions with headroom. Future analyzer-batching work should assume per-pair semantics.
- **#81** (2026-04-26) — schema_discovery_renders_compact_structural_facts_only: Per-analyzer rendering policy is a legitimate optimization surface when an analyzer's result_json contains both structural facts and supporting evidence prose. Future analyzers with similar shapes (verbose evidence + compact structural conclusions) should consider compact rendering at design time, not retrofit. The structural facts must be sufficient for the LLM consumption directive that will read them.
- **#82** (2026-04-26) — pre_s2t_reasoning_business_layer_demoted_based_on_observed_utilization: Layer-budget audits should be informed by observed utilization rather than theoretical allocation guesses. Same pattern likely applies elsewhere: create_s2t has static-layer overflow at ~979 tokens (filed as #97); ontology layer at ~23% utilization across purposes (1740 tokens used of 7500-12000 budget); business layer remains at ~9% utilization even at the new 8333-token cap. Future budget tuning across purposes should anchor on observed data, not theoretical maximums. Filed #97 to capture the create_s2t case explicitly; the broader cross-purpose audit is post-Theme-1 work.
- **#83** (2026-04-26) — runner_attestation_preservation_bug_fixed_post_BG027_diagnostic: Theme 1's audit-discipline thesis is operating correctly. The discovered bug had been silently active since the iteration-attestation gate was added - pre-Theme-1 baseline didn't have the gate so the runner's data-loss was invisible; post-Theme-1 the gate caught its own runner's bug. This kind of latent bug surfacing is the discipline's expected behavior.
- **#84** (2026-04-26) — c5_sourcing_recommendations_partially_viable_validation_layer_required: C5 is technically viable but requires a validation layer between LLM output and analyst-facing recommendations. Three validation options ranked by cost: (a) curated allowlist via sap_data_dictionary.csv extension, (b) tool-augmented prompt with grounded source lookup, (c) empirical pre-flight against existing schema_discovery FK output. Recommended approach: combination of (a) + (c) - allowlist gives high-confidence verified recommendations, empirical cross-check filters hallucinated tables, unverified LLM hypotheses surface separately marked "consult your SAP team." C5 LOC estimate revised from initial ~100-150 to ~200-300 with validation layer. C5 is the right next-move for demo readiness given the reframed value proposition prioritizes "tells you what to ingest" over just "refuses bad SQL".
- **#87** (2026-04-28) — c5_option_b_e2e_validated_on_bg027: SCENARIO ALPHA on first integration run — all four closure-2/4 chain components compose correctly. The full stack from analyzer (Phase 1) through gate (Phase 2) through directive + attestation (Phase 3) through end-to-end + Gap B/C/D fixes (Phase 4) through trigger broadening (Closure 1/4) works on one real LLM call without iteration. Closure 2/4 of 4.
- **#89** (2026-04-28) — c3_c4_empirical_validation_passed_post_phase_2_followup: C3 (TAR attestation) + C4 (Stage A blocker attestation) + Phase 2 follow-up gate fix all empirically validated end-to-end on real LLM. The full architectural arc (data-side gate -> C5 sourcing recs -> needs_data_extension status -> attestation audit trail) works deterministically across both qualified and unqualified outer-filter CTE idioms.
- **#90** (2026-04-29) — c6_empirically_validated_stage_e_refuses_bg027: Architectural arc closes end-to-end. Stage D iteration runner (Option B Phases 1-4 + C5 closure + Theme 1 + Phase 2 follow-up) catches LLM-overconfident-yes deterministically via bridge_coverage_gate with column-lineage tracing. Stage E (C6) honors iteration verdict via BAR-status dispatcher branch + per-call bridge_coverage_gate + DIRECTIVE 3h + bridge_coverage_consulted attestation audit. Demo punchline: BG027 click-through at Stage E now produces structured refusal with sourcing recommendations + reachability violations via _bar_section.render_bar_section, NOT HIGH-confidence wrong SQL. Manual browser visual QA pending (user-driven).
- **#91** (2026-04-29) — lock_demo_happy_path_term_candidate_b_goods_receipts: Candidate B locked as demo's happy-path contrast term. Walk happens in fresh session. Investigation context preserved in an uncommitted session note. Backups identified. This KD captures both the decision and the architectural reasoning (gate-evaluation unity over binary works/refuses framing) so the rationale survives the session boundary.
- **#94** (2026-05-05) — ki96_ghost_resolved_minimal_observability_piece_shipped_pre_demo: KI-96 minimum-fix-shape (logging + footer) shipped; deferred design pass (budget-aware section ordering, budget-aware loader row counts) absorbs into the term-analysis runner redesign scope. Decision: do not pre-implement the deferred shapes ahead of that redesign — risk of throwaway abstraction.

## Open Issues (6)

- **#57** [open/low] check_prerequisites next_steps omits tables with any existing DAR regardless of term context — check_prerequisites filters scope tables to those needing Domain EDA by querying domain_analysis_results WHERE source_tables=<table> AND status='success'. This does not scope by term context — any existing success DAR for a table marks it "done" in next_steps even if the DAR pred…
- **#59** [open/low] Stage B blockers_addressed status picks generous 'addressed' when sibling-DAR context satisfies blocker — Two BG027 smoke-test datapoints (DAR-00058 code_tables, DAR-00059 completeness) both produced status='addressed' for the BWART semantics blocker, but only DAR-00058 directly surfaced BWART distributions. DAR-00059 (completeness) cannot surface distinct values at all — its SQL pro…
- **#69** [open/medium] Post-demo work — term-analysis attestation completeness + architectural hygiene — Series of gaps documented in the post-demo roadmap. Includes: TAR-NNNNN citation format not taught to the term-analysis runner; no `tar_consumed` attestation field; deterministic-analyzer directives missing (date / segmentation / grain_relationship / performance_baseline / schema…
- **#94** [open/low] Scope-aware ordering for the term-analysis DAR loader (Option beta) - deferred follow-up to #93 — During #93 fix design, scope-aware ORDER BY was investigated as Option beta: replace LIMIT 50 ORDER BY executed_at_utc DESC with a scope-overlap-first prioritization (rows whose source_tables overlap the term scope rank above non-overlapping rows; recency within priority). Premis…
- **#95** [open/medium] LIMIT-50 generic DAR cap silently starves entire scope tables when batches run on a subset of scope — Discovered 2026-04-26 during step 4 of Theme 1 (post-C1 + post-#93 verification). For BG027's scope (equi/mseg/mkpf/objk/mard), 12 in-scope EDA DARs sit at ranks 51-62 and never reach the bundle. 11 of the 12 are mard rows; the 12th is objk performance_baseline. All 12 are status…
- **#97** [open/medium] create_s2t static layer overflows budget by ~979 tokens (pre-existing, surfaced during #95 fix-up budget audit) — scripts/_context_assembler.py:_load_static renders the static layer (Layer A semantic_model) at ~8479 tokens for BG027 under purpose=create_s2t against a 7500-token static budget — 979-token overrun. Pre-existing condition, not caused by #95 or its fix-up. Root cause: create_s2t'…
