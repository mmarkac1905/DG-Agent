# Data Vault: hub_design

_Last generated: 2026-07-06 15:06:02_

Keywords: `hub_, business key, golden id, hub design, surrogate key, hub_material, hub_vendor, hub_purchase_order, hub_equipment`

## Designed Hubs (15)

| ID | Name | Business Key | Source Tables | Grain | Notes | Decided |
| --- | --- | --- | --- | --- | --- | --- |
| #1 | `hub_material` | `MATNR` | `MARA` | one row per unique material number | Material is the central CPE entity | 2026-04-13 |
| #2 | `hub_vendor` | `LIFNR` | `LFA1` | one row per unique vendor number | Vendor = equipment supplier | 2026-04-13 |
| #3 | `hub_purchase_order` | `EBELN` | `EKKO` | one row per unique PO number | PO is the procurement transaction key | 2026-04-13 |
| #4 | `hub_equipment` | `EQUNR` | `EQUI` | one row per individual CPE device | Serial-level tracking for CPE lifecycle | 2026-04-13 |
| #5 | `hub_plant` | `WERKS` | `T001W` | one row per plant/warehouse | Organizational entity for stock | 2026-04-13 |
| #18 | `hub_invoice` | `invoice_number` | `RBKP` |  | Hub: Invoice | 2026-04-15 |
| #19 | `hub_material_document` | `material_document_number` | `MKPF` |  | Hub: Material Document | 2026-04-15 |
| #20 | `hub_purchase_requisition` | `requisition_number` | `EBAN` |  | Hub: Purchase Requisition | 2026-04-15 |
| #35 | `hub_accounting_document` | `hk_accounting_document` | `BKPF` |  | Hub: Accounting Document (FI journal entry — WE / RE / RV) | 2026-06-28 |
| #36 | `hub_billing_document` | `hk_billing_doc` | `VBRK` |  | Hub: Billing Document (monthly service invoice) | 2026-06-28 |
| #37 | `hub_customer` | `hk_customer` | `KNA1` |  | Hub: Customer | 2026-06-28 |
| #38 | `hub_movement_type` | `hk_movement_type` | `T156` |  | Hub: Movement Type | 2026-06-28 |
| #39 | `hub_sales_order` | `hk_sales_order` | `VBAK` |  | Hub: Sales Order (service contract) | 2026-06-28 |
| #40 | `hub_service_plan` | `hk_service_plan` | `VBAP` |  | Hub: Service Plan (broadband / IPTV / business subscription) | 2026-06-28 |
| #41 | `hub_zmm_approval` | `hk_zmm_approval` | `ZMM_APPROVAL_LOG` |  | Hub: ZMM Approval (custom Z-table approval events) | 2026-06-28 |


## Related Decisions (8)

- **#7** (2026-04-14) — staging_layer_1to1: Staging follows purist Data Vault approach: mechanical transformation only. Business naming and logic deferred to vault layer. Dropped hk_po_vendor from stg_sap__ekpo — vault-time resolution via EKKO join.
- **#97** (2026-05-05) — rule3_phase2_complete_zero_violations_after_zmm_rseg_vault_build: RULE 3 fully enforced with zero allowlisted exceptions. The checker hard-fails on any new staging-bypass, raw_sap.* direct ref, or sibling-mart ref in marts/obt/knowledge layers. Architecturally the project now has a complete vault footprint for the data sources needed by current marts (procurement, GR, invoicing, custom approvals). Future Z-tables would follow the ZMM pattern: add sources.yml entry -> stg_sap__<table> -> hub/sat/link as appropriate -> mart sources from vault. Worth tracking separately if the BAR-consuming Create S2T flow continues to emit staging-direct refs in auto-generated marts; the checker now blocks them at end_of_task gate.
- **#98** (2026-05-05) — rule_43_codify_facts_carry_hk_plus_natural_keys_not_other_dim_attrs: Codified rather than refactored. RULE 43 is paired with RULE 3 (vault-only refs from marts, enforced by scripts/check_rule3_layer_violations.py) to give the architectural layering a complete written contract: facts ref vault, facts carry hash + natural keys only, no dim attributes leak into facts. If a future code reviewer wants strict Kimball, RULE 43 is the explicit decision to override — they'd need a new decision row supplanting this one. No checker for RULE 43 yet (would need to model the dim-attribute set per dim); could be added if violations surface in practice.
- **#100** (2026-05-05) — vendor_catalog_seed_decommissioned_replaced_by_sat_vendor_business: Phase 1 of the seed-to-vault refactor complete for vendor. Pattern established for the remaining easy case (cpe_catalog -> sat_material_business attached to hub_material) and decision-needed cases (org_structure, abap_logic_catalog, z_tables_catalog — each needs an architectural choice about whether to create a new hub). Production rollout policy documented in knowledge/seeds_catalog.md: entity enrichment belongs in vault sats off existing hubs, NOT standalone seeds. Synthetic data still synthetic, but the FLOW is now production-shape: a real HT Z-table extract drops into raw_sap.zmm_vendor_business and everything downstream works without architecture changes.
- **#101** (2026-05-05) — cpe_catalog_seed_decommissioned_replaced_by_sat_material_business: Phase 2 of the seed-to-vault refactor complete. Vendor + CPE business enrichment now both live in vault sats parented to their respective hubs, joinable by the LLM with one query. Two silent bugs fixed as byproduct. Pattern is now mature for any future Z-table-style entity enrichment. Remaining catalogs (org_structure, abap_logic_catalog, z_tables_catalog) need architectural decisions about new hubs and should be tackled separately. Production rollout: real HT Z-table extracts replace the generator-emitted raw_sap.zmm_*_business tables; everything downstream works unchanged.
- **#104** (2026-05-05) — phase_alpha_movement_type_seed_replaced_by_sap_native_t156_t156t_vault: Movement-type SAP-native refactor complete. Pattern established: enrich synthetic SAP to match real SAP, route through standard tables, derive HT classifications as mart logic. dim_movement_type is now the FIRST mart in the project sourced from vault that previously read a seed — fixed a RULE 3 violation that the checker had been missing. Same lesson applies retroactively to the vendor and material migrations: some of the Z-table fields duplicated SAP-native data (lead_time in MARC.PLIFZ, payment in LFB1.ZTERM, etc.). Next: slim those Z-tables to genuinely-HT-domain fields only.
- **#105** (2026-05-05) — phase_beta_slim_vendor_material_z_tables_to_genuinely_ht_fields_only: This closes the SAP-native-default cleanup cycle the user requested with 'why dont we keep with true SAP native fields mostly'. Z-tables now carry ONLY fields with no SAP home: vendor (equipment_types - HT CPE taxonomy, quality_rating - HT internal scoring, contract_status - HT lifecycle), material (lifecycle_months - HT warranty/TCO horizon, primary_vendor_id - HT preferred supplier; SAP EORD can list many sources without designating a primary). Lesson generalized in feedback_default_sap_native memory: default assumption is real SAP carries the data; invent only for genuinely HT-domain content. Remaining seeds (org_structure, abap_logic_catalog, z_tables_catalog) still need their own architectural decisions - out of scope for this pass.
- **#106** (2026-05-12) — phase_gamma_pre_public_release_drop_decorative_seeds: Pre-public-release cleanup pass 1 of main_seeds complete. The drop list started at 7 candidates and narrowed to 3 after sharper auditing; the reversal kept 4 seeds that initially looked decorative but actually drive UI behavior. Remaining hand-maintained seeds (procurement_rules, org_structure, abap_logic_catalog, z_tables_catalog, zmm_approval_status, zmm_reason_codes, data_contracts, data_vault_design) all have either load-bearing readers or auto-sync mechanisms. Followup: data_vault_design is currently stale by 8 entities (hub_zmm_approval, sat_zmm_approval, sat_invoice_item, hub/sat/sat_text_movement_type, sat_vendor_business, sat_material_business) because end_of_task.py was bypassed on recent phase commits; the sync function in scan_dbt_models.py:sync_vault_design_seed exists and runs on end_of_task.

## Related Domain Relationships (0)

_(none)_

## Open Issues (0)

_(none)_
