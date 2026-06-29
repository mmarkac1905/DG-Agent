# Data Vault: satellite_design

_Last generated: 2026-06-30 00:25:45_

Keywords: `sat_, satellite, hashdiff, descriptive, satellite design, sat_material, sat_vendor, sat_po`

## Designed Satellites (28)

| ID | Name | Business Key | Source Tables | Grain | Notes | Decided |
| --- | --- | --- | --- | --- | --- | --- |
| #10 | `sat_material_general` | `hub_material_key` | `MARA` | Material core attributes | Weight material_group material_type base_uom | 2026-04-13 |
| #11 | `sat_material_description` | `hub_material_key` | `MAKT` | Material descriptions per language | Long text description by language key | 2026-04-13 |
| #12 | `sat_vendor_general` | `hub_vendor_key` | `LFA1` | Vendor general data | Name country city address postal_code | 2026-04-13 |
| #13 | `sat_vendor_commercial` | `hub_vendor_key` | `LFB1` | Vendor commercial data | Payment terms reconciliation account company code | 2026-04-13 |
| #14 | `sat_po_header` | `hub_purchase_order_key` | `EKKO` | PO header attributes | PO date type status purchasing_org | 2026-04-13 |
| #15 | `sat_po_item` | `hub_purchase_order_key` | `EKPO` | PO line item details | Material quantity price plant | 2026-04-13 |
| #16 | `sat_equipment_status` | `hub_equipment_key` | `EQUI+EQBS` | CPE device status over time | Installed/returned/defective/in-stock with dates | 2026-04-13 |
| #17 | `sat_stock_level` | `hub_material_key+hub_plant_key` | `MARD` | Current stock quantities | Unrestricted blocked quality inspection stock | 2026-04-13 |
| #26 | `sat_equipment_general` | `hk_equipment` | `EQUI` |  | Satellite: Equipment General (static device info) | 2026-04-15 |
| #27 | `sat_gr_header` | `hk_material_document` | `MKPF` |  | Satellite: GR / Material Document Header | 2026-04-15 |
| #28 | `sat_gr_item` | `document_item` | `MSEG` |  | Satellite: Material Document Item | 2026-04-15 |
| #29 | `sat_invoice_header` | `hk_invoice` | `RBKP` |  | Satellite: Invoice Header | 2026-04-15 |
| #30 | `sat_material_plant` | `hk_material_plant` | `MARC` |  | Satellite: Material Plant-Level Data (MRP settings, serial profile, lead time) | 2026-04-15 |
| #31 | `sat_po_account` | `hk_po_item` | `EKKN` |  | Satellite: PO Account Assignment | 2026-04-15 |
| #32 | `sat_po_schedule` | `hk_po_item` | `EKET` |  | Satellite: PO Delivery Schedule | 2026-04-15 |
| #33 | `sat_pr_detail` | `hk_purchase_requisition` | `EBAN` |  | Satellite: Purchase Requisition Detail | 2026-04-15 |
| #49 | `sat_accounting_header` | `hk_accounting_document` | `BKPF` |  | Satellite: Accounting Document Header (FI journal — WE / RE / RV) | 2026-06-28 |
| #50 | `sat_billing_document` | `hk_billing_doc` | `VBRK` |  | Satellite: Billing Document (monthly service invoice) | 2026-06-28 |
| #51 | `sat_customer` | `hk_customer` | `KNA1` |  | Satellite: Customer | 2026-06-28 |
| #52 | `sat_invoice_item` | `hk_invoice` | `RSEG` |  | Satellite: Invoice Item (RSEG line-item details) | 2026-06-28 |
| #53 | `sat_material_business` | `hk_material` | `ZMM_MATERIAL_BUSINESS` |  | Satellite: Material Business Enrichment (HT-domain) | 2026-06-28 |
| #54 | `sat_movement_type` | `hk_movement_type` | `T156` |  | Satellite: Movement Type Configuration | 2026-06-28 |
| #55 | `sat_movement_type_text` | `hk_movement_type` | `T156T` |  | Satellite: Movement Type Text (multi-language) | 2026-06-28 |
| #56 | `sat_sales_order` | `hk_sales_order` | `VBAK` |  | Satellite: Sales Order (contract header) | 2026-06-28 |
| #57 | `sat_sales_order_item` | `service_plan_id` | `VBAP` |  | Satellite: Sales Order Item (service line + device) | 2026-06-28 |
| #58 | `sat_service_plan` | `hk_service_plan` | `VBAP` |  | Satellite: Service Plan | 2026-06-28 |
| #59 | `sat_vendor_business` | `hk_vendor` | `ZMM_VENDOR_BUSINESS` |  | Satellite: Vendor Business Enrichment (HT-domain) | 2026-06-28 |
| #60 | `sat_zmm_approval` | `hk_zmm_approval` | `ZMM_APPROVAL_LOG` |  | Satellite: ZMM Approval Attributes | 2026-06-28 |


## Related Decisions (14)

- **#7** (2026-04-14) — staging_layer_1to1: Staging follows purist Data Vault approach: mechanical transformation only. Business naming and logic deferred to vault layer. Dropped hk_po_vendor from stg_sap__ekpo — vault-time resolution via EKKO join.
- **#8** (2026-04-14) — data_vault_layer_built: Data Vault complete. 33 incremental models (spec said 32 but listed 16 satellites so actual total is 33). Hubs insert-only. Satellites use hashdiff for SCD2 change detection. Fixed link_equipment_gr to hash MBLNR+MJAHR via SERI->MKPF join so hk_material_document matches hub definition. Ready for marts layer.
- **#9** (2026-04-14) — marts_and_obt_built: Full analytical stack complete: raw -> staging -> vault -> marts -> OBT. Fixed three spec bugs: hk_po_item vs hk_po_material mismatch gr_totals hk_material_document join fact_invoices hk_vendor not in sat.
- **#17** (2026-04-15) **[NEVER_REPEAT]** — link_po_item_unit_of_work: Every PO-item grain satellite must hang off link_po_item. Never create a satellite parented on a hash key that does not exist as a PK in a hub or link.
- **#71** (2026-04-20) **[REVERTED]** — gate_c_elikz_null_injection_for_loop_closure_test: reverted=false. Per user directive, revert runs ONLY after explicit confirmation post-Gate-C, regardless of pass or fail. If Gate C fails, null-injected state is preserved for debug inspection. Once user authorizes revert, bump this row's reverted=true and append a revert-completion decision.
- **#96** (2026-05-05) — rule3_enforcement_checker_plus_two_mart_refactors: RULE 3 now has a checker. Two of three violators refactored to vault-sourced patterns matching fact_purchase_orders / fact_invoices conventions. The third is allowlisted with a clear fix path (KI-121: build sat_invoice_item + zmm_approval_log staging+vault, then refactor). Future model changes that introduce new RULE 3 violations will hard-fail end_of_task.py.
- **#97** (2026-05-05) — rule3_phase2_complete_zero_violations_after_zmm_rseg_vault_build: RULE 3 fully enforced with zero allowlisted exceptions. The checker hard-fails on any new staging-bypass, raw_sap.* direct ref, or sibling-mart ref in marts/obt/knowledge layers. Architecturally the project now has a complete vault footprint for the data sources needed by current marts (procurement, GR, invoicing, custom approvals). Future Z-tables would follow the ZMM pattern: add sources.yml entry -> stg_sap__<table> -> hub/sat/link as appropriate -> mart sources from vault. Worth tracking separately if the Piece 8.5 Create S2T flow continues to emit staging-direct refs in auto-generated marts; the checker now blocks them at end_of_task gate.
- **#100** (2026-05-05) — vendor_catalog_seed_decommissioned_replaced_by_sat_vendor_business: Phase 1 of the seed-to-vault refactor complete for vendor. Pattern established for the remaining easy case (cpe_catalog -> sat_material_business attached to hub_material) and decision-needed cases (org_structure, abap_logic_catalog, z_tables_catalog — each needs an architectural choice about whether to create a new hub). Production rollout policy documented in knowledge/seeds_catalog.md: entity enrichment belongs in vault sats off existing hubs, NOT standalone seeds. Synthetic data still synthetic, but the FLOW is now production-shape: a real HT Z-table extract drops into raw_sap.zmm_vendor_business and everything downstream works without architecture changes.
- **#101** (2026-05-05) — cpe_catalog_seed_decommissioned_replaced_by_sat_material_business: Phase 2 of the seed-to-vault refactor complete. Vendor + CPE business enrichment now both live in vault sats parented to their respective hubs, joinable by the LLM with one query. Two silent bugs fixed as byproduct. Pattern is now mature for any future Z-table-style entity enrichment. Remaining catalogs (org_structure, abap_logic_catalog, z_tables_catalog) need architectural decisions about new hubs and should be tackled separately. Production rollout: real HT Z-table extracts replace the generator-emitted raw_sap.zmm_*_business tables; everything downstream works unchanged.
- **#104** (2026-05-05) — phase_alpha_movement_type_seed_replaced_by_sap_native_t156_t156t_vault: Phase α complete. Pattern established: enrich synthetic SAP to match real SAP, route through standard tables, derive HT classifications as mart logic. dim_movement_type is now the FIRST mart in the project sourced from vault that previously read a seed — fixed a RULE 3 violation that the checker had been missing. Same lesson applies retroactively to Phases 1+2 (vendor, material): some of the Z-table fields duplicated SAP-native data (lead_time in MARC.PLIFZ, payment in LFB1.ZTERM, etc.). Phase β next: slim those Z-tables to genuinely-HT-domain fields only.
- **#105** (2026-05-05) — phase_beta_slim_vendor_material_z_tables_to_genuinely_ht_fields_only: Phase beta closes the SAP-native-default cleanup cycle the user requested with 'why dont we keep with true SAP native fields mostly'. Z-tables now carry ONLY fields with no SAP home: vendor (equipment_types - HT CPE taxonomy, quality_rating - HT internal scoring, contract_status - HT lifecycle), material (lifecycle_months - HT warranty/TCO horizon, primary_vendor_id - HT preferred supplier; SAP EORD can list many sources without designating a primary). Lesson generalized in feedback_default_sap_native memory: default assumption is real SAP carries the data; invent only for genuinely HT-domain content. Remaining seeds (org_structure, abap_logic_catalog, z_tables_catalog) still need their own architectural decisions - out of scope for this pass.
- **#106** (2026-05-12) — phase_gamma_pre_public_release_drop_decorative_seeds: Pre-public-release cleanup pass 1 of main_seeds complete. The drop list started at 7 candidates and narrowed to 3 after sharper auditing; the reversal kept 4 seeds that initially looked decorative but actually drive UI behavior. Remaining hand-maintained seeds (procurement_rules, org_structure, abap_logic_catalog, z_tables_catalog, zmm_approval_status, zmm_reason_codes, data_contracts, data_vault_design) all have either load-bearing readers or auto-sync mechanisms. Followup: data_vault_design is currently stale by 8 entities (hub_zmm_approval, sat_zmm_approval, sat_invoice_item, hub/sat/sat_text_movement_type, sat_vendor_business, sat_material_business) because end_of_task.py was bypassed on recent phase commits; the sync function in scan_dbt_models.py:sync_vault_design_seed exists and runs on end_of_task.
- **#112** (2026-06-25) — path3_sd_fi_infra_build: SD + FI are now first-class in the warehouse. Margin term cost side v1 = revenue - procurement cost; returns (MSEG 161/122 are DMBTR=0, unvalued) and warranty (ZHT_WARRANTY_LOG catalog-only, no data) deferred. Reference: scripts/generate_sd_billing.py, scripts/generate_fi_shadows.py, dbt/models/vault/link_sales_order_equipment.sql.
- **#122** (2026-06-28) **[NEVER_REPEAT]** — bg030_mart_refactored_to_vault_layer: Marts must build on the vault layer; enforce the layering rule at GENERATION (prompt), not only at the commit gate.

## Related Domain Relationships (0)

_(none)_

## Open Issues (1)

- **#24** [open/low] Hashdiff macro treats NULL and empty-string as identical, suppressing SCD2 expansion for ELIKZ-class nullable columns — Observed 2026-04-20 during Phase 15a piece 5 Gate C (ELIKZ null-injection for loop-closure validation; decision #71). Sequence: (a) Pre-injection raw_sap.ekpo had 2200 rows with ELIKZ uniformly empty string ''. (b) Injected 110 explicit NULLs into ELIKZ (5%) via deterministic sel…

## DO NOT (Anti-patterns)

- **#17** (2026-04-15) **[NEVER_REPEAT]** — link_po_item_unit_of_work: Every PO-item grain satellite must hang off link_po_item. Never create a satellite parented on a hash key that does not exist as a PK in a hub or link.
- **#71** (2026-04-20) **[REVERTED]** — gate_c_elikz_null_injection_for_loop_closure_test: reverted=false. Per user directive, revert runs ONLY after explicit confirmation post-Gate-C, regardless of pass or fail. If Gate C fails, null-injected state is preserved for debug inspection. Once user authorizes revert, bump this row's reverted=true and append a revert-completion decision.
- **#122** (2026-06-28) **[NEVER_REPEAT]** — bg030_mart_refactored_to_vault_layer: Marts must build on the vault layer; enforce the layering rule at GENERATION (prompt), not only at the commit gate.
