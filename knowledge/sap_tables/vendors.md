# SAP Tables: vendors

_Last generated: 2026-07-07 11:33:06_

Keywords: `lfa1, lfb1, lfm1, vendor master, supplier, lifnr, dobavljač, vendor_eval, vendor_score, ZHT_VENDOR_SCORES, ZHT_VENDOR_SPEND`

## Related Decisions (6)

- **#10** (2026-04-14) — streamlit_dashboard_built: Dashboard MVP complete. Moved pandas import to file top on procurement_overview page (spec had it after first pd.notna call which would have crashed). Installed streamlit and plotly in venv.
- **#63** (2026-04-18) **[NEVER_REPEAT]** — semantic_validation_gate_phase14: Semantic validation belongs inside the Deploy flow, not downstream. Compile-time green means nothing for business-correctness. When cost-bounding the validator: only deterministic dimensions that can be checked against declared term attributes (grain, filter, unit). Skip definition drift — it is subjective and overlaps with the other three. Bias toward false negatives (accepting ambiguous cases) since blocking a legitimate Deploy is much worse than letting one through. The live V3 test exposing the cpe_active_deployed_count grain bug validates the choice to ship this gate. See RULE 40.
- **#100** (2026-05-05) — vendor_catalog_seed_decommissioned_replaced_by_sat_vendor_business: Phase 1 of the seed-to-vault refactor complete for vendor. Pattern established for the remaining easy case (cpe_catalog -> sat_material_business attached to hub_material) and decision-needed cases (org_structure, abap_logic_catalog, z_tables_catalog — each needs an architectural choice about whether to create a new hub). Production rollout policy documented in knowledge/seeds_catalog.md: entity enrichment belongs in vault sats off existing hubs, NOT standalone seeds. Synthetic data still synthetic, but the FLOW is now production-shape: a real HT Z-table extract drops into raw_sap.zmm_vendor_business and everything downstream works without architecture changes.
- **#104** (2026-05-05) — phase_alpha_movement_type_seed_replaced_by_sap_native_t156_t156t_vault: Movement-type SAP-native refactor complete. Pattern established: enrich synthetic SAP to match real SAP, route through standard tables, derive HT classifications as mart logic. dim_movement_type is now the FIRST mart in the project sourced from vault that previously read a seed — fixed a RULE 3 violation that the checker had been missing. Same lesson applies retroactively to the vendor and material migrations: some of the Z-table fields duplicated SAP-native data (lead_time in MARC.PLIFZ, payment in LFB1.ZTERM, etc.). Next: slim those Z-tables to genuinely-HT-domain fields only.
- **#105** (2026-05-05) — phase_beta_slim_vendor_material_z_tables_to_genuinely_ht_fields_only: This closes the SAP-native-default cleanup cycle the user requested with 'why dont we keep with true SAP native fields mostly'. Z-tables now carry ONLY fields with no SAP home: vendor (equipment_types - HT CPE taxonomy, quality_rating - HT internal scoring, contract_status - HT lifecycle), material (lifecycle_months - HT warranty/TCO horizon, primary_vendor_id - HT preferred supplier; SAP EORD can list many sources without designating a primary). Lesson generalized in feedback_default_sap_native memory: default assumption is real SAP carries the data; invent only for genuinely HT-domain content. Remaining seeds (org_structure, abap_logic_catalog, z_tables_catalog) still need their own architectural decisions - out of scope for this pass.
- **#108** (2026-05-12) **[NEVER_REPEAT]** — vendor_country_appears_not_to_affect_defect_rate_but_query_was_broken: DO NOT use obt_procurement_overview alone for vendor attribution of equipment outcomes — the material_number join is many-to-many across vendors. Correct path: traverse link_equipment_gr -> link_gr_po -> link_po_vendor in the vault, or build a fact_equipment_with_vendor mart that follows that chain. Migrated from decommissioned signal_relationships #6.

## Related Domain Relationships (0)

_(none)_

## Open Issues (0)

_(none)_

## DO NOT (Anti-patterns)

- **#63** (2026-04-18) **[NEVER_REPEAT]** — semantic_validation_gate_phase14: Semantic validation belongs inside the Deploy flow, not downstream. Compile-time green means nothing for business-correctness. When cost-bounding the validator: only deterministic dimensions that can be checked against declared term attributes (grain, filter, unit). Skip definition drift — it is subjective and overlaps with the other three. Bias toward false negatives (accepting ambiguous cases) since blocking a legitimate Deploy is much worse than letting one through. The live V3 test exposing the cpe_active_deployed_count grain bug validates the choice to ship this gate. See RULE 40.
- **#108** (2026-05-12) **[NEVER_REPEAT]** — vendor_country_appears_not_to_affect_defect_rate_but_query_was_broken: DO NOT use obt_procurement_overview alone for vendor attribution of equipment outcomes — the material_number join is many-to-many across vendors. Correct path: traverse link_equipment_gr -> link_gr_po -> link_po_vendor in the vault, or build a fact_equipment_with_vendor mart that follows that chain. Migrated from decommissioned signal_relationships #6.
