# SAP Tables: purchase_orders

_Last generated: 2026-07-06 10:08:36_

Keywords: `ekko, ekpo, eket, ekkn, ekbe, purchase order, narudžbenica, po header, po item, dual_source, ZMM_DUAL_SOURCE, cost_center_derive`

## Related Decisions (14)

- **#4** (2026-04-14) — serial_tracking_tables_added: Data Vault link_equipment_po can now be built from OBJK+SER01. 5 tables intentionally empty (MCHB MVKE MSKA LQUA LTAP) — documented in known_decisions.
- **#7** (2026-04-14) — staging_layer_1to1: Staging follows purist Data Vault approach: mechanical transformation only. Business naming and logic deferred to vault layer. Dropped hk_po_vendor from stg_sap__ekpo — vault-time resolution via EKKO join.
- **#11** (2026-04-14) — knowledge_models_built: Intelligence layer complete. Session startup now surfaces live system state with health assessments. Context export ready for chat sessions.
- **#17** (2026-04-15) **[NEVER_REPEAT]** — link_po_item_unit_of_work: Every PO-item grain satellite must hang off link_po_item. Never create a satellite parented on a hash key that does not exist as a PK in a hub or link.
- **#29** (2026-04-17) — llm_prompt_no_passthrough_for_functions: LLM prompt must explicitly forbid pass-through language when SQL contains functions. scanner classification and LLM description are separate concerns — both must be accurate independently.
- **#68** (2026-04-19) — source_column_roles_seed_and_classifier: Pipeline is wired and verified dry-run against live DB without burning LLM tokens. Cold-start execution is gated behind explicit CLI invocation (python scripts/classify_source_columns.py) so the ~$0.18 / ~15-min burn only happens when the user is ready. Once cold-start runs, (a) bump _csv_safeguard floor from 10 to ~280 (99% of observed cold-start row count), (b) wire an optional delta invocation inside end_of_task.py (currently manual) with the 50-row cap intact.
- **#71** (2026-04-20) **[REVERTED]** — gate_c_elikz_null_injection_for_loop_closure_test: reverted=false. Per user directive, revert runs ONLY after explicit confirmation post-test, regardless of pass or fail. If the loop-closure test fails, null-injected state is preserved for debug inspection. Once user authorizes revert, bump this row's reverted=true and append a revert-completion decision.
- **#85** (2026-04-27) — c5_pre_phase_1_experiments_complete: Cost is not the binding constraint for C5. The validation layer's design works as specified. Phase 1 implementation can proceed with empirical foundations rather than placeholders. The pre-Phase-1 gate served its purpose: caught the top-N sampling bias before Phase 1 committed to it as the production approach (Phase 2 fix is now scoped, not retroactive).
- **#91** (2026-04-29) — lock_demo_happy_path_term_candidate_b_goods_receipts: Candidate B locked as demo's happy-path contrast term. Walk happens in fresh session. Investigation context preserved in an uncommitted session note. Backups identified. This KD captures both the decision and the architectural reasoning (gate-evaluation unity over binary works/refuses framing) so the rationale survives the session boundary.
- **#96** (2026-05-05) — rule3_enforcement_checker_plus_two_mart_refactors: RULE 3 now has a checker. Two of three violators refactored to vault-sourced patterns matching fact_purchase_orders / fact_invoices conventions. The third is allowlisted with a clear fix path (KI-121: build sat_invoice_item + zmm_approval_log staging+vault, then refactor). Future model changes that introduce new RULE 3 violations will hard-fail end_of_task.py.
- **#97** (2026-05-05) — rule3_phase2_complete_zero_violations_after_zmm_rseg_vault_build: RULE 3 fully enforced with zero allowlisted exceptions. The checker hard-fails on any new staging-bypass, raw_sap.* direct ref, or sibling-mart ref in marts/obt/knowledge layers. Architecturally the project now has a complete vault footprint for the data sources needed by current marts (procurement, GR, invoicing, custom approvals). Future Z-tables would follow the ZMM pattern: add sources.yml entry -> stg_sap__<table> -> hub/sat/link as appropriate -> mart sources from vault. Worth tracking separately if the BAR-consuming Create S2T flow continues to emit staging-direct refs in auto-generated marts; the checker now blocks them at end_of_task gate.
- **#105** (2026-05-05) — phase_beta_slim_vendor_material_z_tables_to_genuinely_ht_fields_only: This closes the SAP-native-default cleanup cycle the user requested with 'why dont we keep with true SAP native fields mostly'. Z-tables now carry ONLY fields with no SAP home: vendor (equipment_types - HT CPE taxonomy, quality_rating - HT internal scoring, contract_status - HT lifecycle), material (lifecycle_months - HT warranty/TCO horizon, primary_vendor_id - HT preferred supplier; SAP EORD can list many sources without designating a primary). Lesson generalized in feedback_default_sap_native memory: default assumption is real SAP carries the data; invent only for genuinely HT-domain content. Remaining seeds (org_structure, abap_logic_catalog, z_tables_catalog) still need their own architectural decisions - out of scope for this pass.
- **#119** (2026-06-28) **[NEVER_REPEAT]** — deployment_date_coalesce_inbdt_then_first_bill: When deployment movements are not serial-linked, first service-bill date is the most defensible deployment proxy (aligns cost recognition with revenue). Resolves the INBDT-null analyst_decision blocker.
- **#120** (2026-06-28) **[NEVER_REPEAT]** — drop_returns_leg_unvalued_in_data: Drop a margin component when the source movements carry no valuation. Like warranty earlier - synthetic data only values inbound (101) movements.

## Related Domain Relationships (0)

_(none)_

## Open Issues (1)

- **#24** [open/low] Hashdiff macro treats NULL and empty-string as identical, suppressing SCD2 expansion for ELIKZ-class nullable columns — Observed 2026-04-20 during the ELIKZ null-injection loop-closure validation (decision #71). Sequence: (a) Pre-injection raw_sap.ekpo had 2200 rows with ELIKZ uniformly empty string ''. (b) Injected 110 explicit NULLs into ELIKZ (5%) via deterministic selector. (c) main_staging.st…

## DO NOT (Anti-patterns)

- **#17** (2026-04-15) **[NEVER_REPEAT]** — link_po_item_unit_of_work: Every PO-item grain satellite must hang off link_po_item. Never create a satellite parented on a hash key that does not exist as a PK in a hub or link.
- **#71** (2026-04-20) **[REVERTED]** — gate_c_elikz_null_injection_for_loop_closure_test: reverted=false. Per user directive, revert runs ONLY after explicit confirmation post-test, regardless of pass or fail. If the loop-closure test fails, null-injected state is preserved for debug inspection. Once user authorizes revert, bump this row's reverted=true and append a revert-completion decision.
- **#119** (2026-06-28) **[NEVER_REPEAT]** — deployment_date_coalesce_inbdt_then_first_bill: When deployment movements are not serial-linked, first service-bill date is the most defensible deployment proxy (aligns cost recognition with revenue). Resolves the INBDT-null analyst_decision blocker.
- **#120** (2026-06-28) **[NEVER_REPEAT]** — drop_returns_leg_unvalued_in_data: Drop a margin component when the source movements carry no valuation. Like warranty earlier - synthetic data only values inbound (101) movements.
