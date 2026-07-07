# SAP Tables: org_structure

_Last generated: 2026-07-07 12:05:18_

Keywords: `t001, t001w, t001l, t024, company code, plant, storage location, purchasing org`

## Related Decisions (4)

- **#34** (2026-04-17) **[NEVER_REPEAT]** — guided_bt_also_gets_domain_facts_injection: Injection is scope-filtered not content-filtered. Goal is relevant context not minimum context. Five other claude_api.py call sites and sync_s2t_plain_from_dbt.py remain excluded due to narrow scope or cache economics.
- **#63** (2026-04-18) **[NEVER_REPEAT]** — semantic_validation_gate_phase14: Semantic validation belongs inside the Deploy flow, not downstream. Compile-time green means nothing for business-correctness. When cost-bounding the validator: only deterministic dimensions that can be checked against declared term attributes (grain, filter, unit). Skip definition drift — it is subjective and overlaps with the other three. Bias toward false negatives (accepting ambiguous cases) since blocking a legitimate Deploy is much worse than letting one through. The live V3 test exposing the cpe_active_deployed_count grain bug validates the choice to ship this gate. See RULE 40.
- **#98** (2026-05-05) — rule_43_codify_facts_carry_hk_plus_natural_keys_not_other_dim_attrs: Codified rather than refactored. RULE 43 is paired with RULE 3 (vault-only refs from marts, enforced by scripts/check_rule3_layer_violations.py) to give the architectural layering a complete written contract: facts ref vault, facts carry hash + natural keys only, no dim attributes leak into facts. If a future code reviewer wants strict Kimball, RULE 43 is the explicit decision to override — they'd need a new decision row supplanting this one. No checker for RULE 43 yet (would need to model the dim-attribute set per dim); could be added if violations surface in practice.
- **#105** (2026-05-05) — phase_beta_slim_vendor_material_z_tables_to_genuinely_ht_fields_only: This closes the SAP-native-default cleanup cycle the user requested with 'why dont we keep with true SAP native fields mostly'. Z-tables now carry ONLY fields with no SAP home: vendor (equipment_types - HT CPE taxonomy, quality_rating - HT internal scoring, contract_status - HT lifecycle), material (lifecycle_months - HT warranty/TCO horizon, primary_vendor_id - HT preferred supplier; SAP EORD can list many sources without designating a primary). Lesson generalized in feedback_default_sap_native memory: default assumption is real SAP carries the data; invent only for genuinely HT-domain content. Remaining seeds (org_structure, abap_logic_catalog, z_tables_catalog) still need their own architectural decisions - out of scope for this pass.

## Related Domain Relationships (0)

_(none)_

## Open Issues (2)

- **#4** [open/low] Inventory MoS values unrealistic in sample data — Q7 discovery query showed months-of-stock ranging 650-6455 months across all materials/plants. Generator GR inflow far exceeds deployment outflow (~45K serial-tracked deployments vs ~180K received qty). MARD stock computed from net movements so it balloons. Real HT data would sho…
- **#95** [open/medium] LIMIT-50 generic DAR cap silently starves entire scope tables when batches run on a subset of scope — Discovered 2026-04-26 during step 4 of Theme 1 (post-C1 + post-#93 verification). For BG027's scope (equi/mseg/mkpf/objk/mard), 12 in-scope EDA DARs sit at ranks 51-62 and never reach the bundle. 11 of the 12 are mard rows; the 12th is objk performance_baseline. All 12 are status…

## DO NOT (Anti-patterns)

- **#34** (2026-04-17) **[NEVER_REPEAT]** — guided_bt_also_gets_domain_facts_injection: Injection is scope-filtered not content-filtered. Goal is relevant context not minimum context. Five other claude_api.py call sites and sync_s2t_plain_from_dbt.py remain excluded due to narrow scope or cache economics.
- **#63** (2026-04-18) **[NEVER_REPEAT]** — semantic_validation_gate_phase14: Semantic validation belongs inside the Deploy flow, not downstream. Compile-time green means nothing for business-correctness. When cost-bounding the validator: only deterministic dimensions that can be checked against declared term attributes (grain, filter, unit). Skip definition drift — it is subjective and overlaps with the other three. Bias toward false negatives (accepting ambiguous cases) since blocking a legitimate Deploy is much worse than letting one through. The live V3 test exposing the cpe_active_deployed_count grain bug validates the choice to ship this gate. See RULE 40.
