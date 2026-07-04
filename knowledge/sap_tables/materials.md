# SAP Tables: materials

_Last generated: 2026-07-04 02:20:08_

Keywords: `mara, makt, marc, marm, mvke, material master, material number, matnr, materijal`

## Related Decisions (9)

- **#4** (2026-04-14) — serial_tracking_tables_added: Data Vault link_equipment_po can now be built from OBJK+SER01. 5 tables intentionally empty (MCHB MVKE MSKA LQUA LTAP) — documented in known_decisions.
- **#5** (2026-04-14) — empty_tables_documented: Tables exist for schema completeness and data dictionary validity. Will be populated if scope expands to include WM or SD modules.
- **#85** (2026-04-27) — c5_pre_phase_1_experiments_complete: Cost is not the binding constraint for C5. The validation layer's design works as specified. Phase 1 implementation can proceed with empirical foundations rather than placeholders. The pre-Phase-1 gate served its purpose: caught the top-N sampling bias before Phase 1 committed to it as the production approach (Phase 2 fix is now scoped, not retroactive).
- **#98** (2026-05-05) — rule_43_codify_facts_carry_hk_plus_natural_keys_not_other_dim_attrs: Codified rather than refactored. RULE 43 is paired with RULE 3 (vault-only refs from marts, enforced by scripts/check_rule3_layer_violations.py) to give the architectural layering a complete written contract: facts ref vault, facts carry hash + natural keys only, no dim attributes leak into facts. If a future code reviewer wants strict Kimball, RULE 43 is the explicit decision to override — they'd need a new decision row supplanting this one. No checker for RULE 43 yet (would need to model the dim-attribute set per dim); could be added if violations surface in practice.
- **#101** (2026-05-05) — cpe_catalog_seed_decommissioned_replaced_by_sat_material_business: Phase 2 of the seed-to-vault refactor complete. Vendor + CPE business enrichment now both live in vault sats parented to their respective hubs, joinable by the LLM with one query. Two silent bugs fixed as byproduct. Pattern is now mature for any future Z-table-style entity enrichment. Remaining catalogs (org_structure, abap_logic_catalog, z_tables_catalog) need architectural decisions about new hubs and should be tackled separately. Production rollout: real HT Z-table extracts replace the generator-emitted raw_sap.zmm_*_business tables; everything downstream works unchanged.
- **#104** (2026-05-05) — phase_alpha_movement_type_seed_replaced_by_sap_native_t156_t156t_vault: Movement-type SAP-native refactor complete. Pattern established: enrich synthetic SAP to match real SAP, route through standard tables, derive HT classifications as mart logic. dim_movement_type is now the FIRST mart in the project sourced from vault that previously read a seed — fixed a RULE 3 violation that the checker had been missing. Same lesson applies retroactively to the vendor and material migrations: some of the Z-table fields duplicated SAP-native data (lead_time in MARC.PLIFZ, payment in LFB1.ZTERM, etc.). Next: slim those Z-tables to genuinely-HT-domain fields only.
- **#105** (2026-05-05) — phase_beta_slim_vendor_material_z_tables_to_genuinely_ht_fields_only: This closes the SAP-native-default cleanup cycle the user requested with 'why dont we keep with true SAP native fields mostly'. Z-tables now carry ONLY fields with no SAP home: vendor (equipment_types - HT CPE taxonomy, quality_rating - HT internal scoring, contract_status - HT lifecycle), material (lifecycle_months - HT warranty/TCO horizon, primary_vendor_id - HT preferred supplier; SAP EORD can list many sources without designating a primary). Lesson generalized in feedback_default_sap_native memory: default assumption is real SAP carries the data; invent only for genuinely HT-domain content. Remaining seeds (org_structure, abap_logic_catalog, z_tables_catalog) still need their own architectural decisions - out of scope for this pass.
- **#108** (2026-05-12) **[NEVER_REPEAT]** — vendor_country_appears_not_to_affect_defect_rate_but_query_was_broken: DO NOT use obt_procurement_overview alone for vendor attribution of equipment outcomes — the material_number join is many-to-many across vendors. Correct path: traverse link_equipment_gr -> link_gr_po -> link_po_vendor in the vault, or build a fact_equipment_with_vendor mart that follows that chain. Migrated from decommissioned signal_relationships #6.
- **#117** (2026-06-27) **[NEVER_REPEAT]** — term_eda_requires_full_scope_domain_eda: Prefer minimal scope. A wide term incurs quadratic grain_relationship EDA cost before Term EDA can even start.

## Related Domain Relationships (0)

_(none)_

## Open Issues (2)

- **#5** [open/medium] Vendor-equipment attribution requires vault traceability — Q4 discovery showed obt_procurement_overview alone cannot attribute equipment outcomes to vendors because multiple vendors share material numbers (join fan-out). Need a fact_equipment_with_vendor model built from link_equipment_gr -> link_gr_po -> link_po_vendor. Backlog for next…
- **#95** [open/medium] LIMIT-50 generic DAR cap silently starves entire scope tables when batches run on a subset of scope — Discovered 2026-04-26 during step 4 of Theme 1 (post-C1 + post-#93 verification). For BG027's scope (equi/mseg/mkpf/objk/mard), 12 in-scope EDA DARs sit at ranks 51-62 and never reach the bundle. 11 of the 12 are mard rows; the 12th is objk performance_baseline. All 12 are status…

## DO NOT (Anti-patterns)

- **#108** (2026-05-12) **[NEVER_REPEAT]** — vendor_country_appears_not_to_affect_defect_rate_but_query_was_broken: DO NOT use obt_procurement_overview alone for vendor attribution of equipment outcomes — the material_number join is many-to-many across vendors. Correct path: traverse link_equipment_gr -> link_gr_po -> link_po_vendor in the vault, or build a fact_equipment_with_vendor mart that follows that chain. Migrated from decommissioned signal_relationships #6.
- **#117** (2026-06-27) **[NEVER_REPEAT]** — term_eda_requires_full_scope_domain_eda: Prefer minimal scope. A wide term incurs quadratic grain_relationship EDA cost before Term EDA can even start.
