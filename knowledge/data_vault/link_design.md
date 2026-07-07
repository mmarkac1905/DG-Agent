# Data Vault: link_design

_Last generated: 2026-07-07 11:00:29_

Keywords: `link_, relationship, link design, many-to-many, link_po_vendor, link_po_material, link_gr_po`

## Designed Links (17)

| ID | Name | Business Key | Source Tables | Grain | Notes | Decided |
| --- | --- | --- | --- | --- | --- | --- |
| #6 | `link_po_vendor` | `EBELN+LIFNR` | `EKKO` | PO-to-vendor relationship | Which vendor supplies which PO | 2026-04-13 |
| #7 | `link_po_material` | `EBELN+EBELP+MATNR` | `EKPO` | PO item-to-material relationship | What materials are on each PO | 2026-04-13 |
| #8 | `link_gr_po` | `MBLNR+EBELN` | `MSEG` | Goods receipt-to-PO relationship | Which GR fulfills which PO | 2026-04-13 |
| #9 | `link_equipment_material` | `EQUNR+MATNR` | `EQUI` | Device-to-material type relationship | Which material type is this specific device | 2026-04-13 |
| #21 | `link_equipment_gr` | `hk_equipment_gr` | `MKPF+SERI` |  | Link: Equipment ↔ Material Document (GR) | 2026-04-15 |
| #22 | `link_gr_material` | `hk_gr_material` | `MSEG` |  | Link: Goods Receipt ↔ Material | 2026-04-15 |
| #23 | `link_invoice_po` | `hk_invoice_po` | `RBKP` |  | Link: Invoice ↔ Purchase Order | 2026-04-15 |
| #24 | `link_po_plant` | `hk_po_plant` | `EKPO` |  | Link: Purchase Order ↔ Plant | 2026-04-15 |
| #25 | `link_pr_po` | `hk_pr_po` | `EKPO` |  | Link: Purchase Requisition ↔ Purchase Order | 2026-04-15 |
| #34 | `link_po_item` | `hk_purchase_order` | `EKPO` |  | Link: Purchase Order Item | 2026-04-15 |
| #42 | `link_billing_accounting` | `hk_billing_doc` | `BKPF+VBRK` |  | FI Link 2/3: SD Billing ↔ Accounting Document (revenue side, BLART='RV') | 2026-06-28 |
| #43 | `link_billing_customer` | `hk_billing_doc` | `VBRK` |  | Link: Billing Document ↔ Customer (who is billed — payer) | 2026-06-28 |
| #44 | `link_billing_sales_order` | `hk_billing_doc` | `VBRP` |  | Link: Billing Document ↔ Sales Order (bill back to the contract) | 2026-06-28 |
| #45 | `link_gr_accounting` | `hk_material_document` | `BKPF+MKPF` |  | FI Link 3/3: Goods Receipt ↔ Accounting Document (inventory side, BLART='WE') | 2026-06-28 |
| #46 | `link_invoice_accounting` | `hk_invoice` | `BKPF+RBKP` |  | FI Link 1/3: MM Invoice ↔ Accounting Document (cost side, BLART='RE') | 2026-06-28 |
| #47 | `link_sales_order_customer` | `hk_sales_order` | `VBAK` |  | Link: Sales Order ↔ Customer (who holds the contract) | 2026-06-28 |
| #48 | `link_sales_order_equipment` | `hk_sales_order` | `EQUI+VBAP` |  | Link: Sales Order Item ↔ Equipment (the marquee tie) | 2026-06-28 |


## Related Decisions (15)

- **#4** (2026-04-14) — serial_tracking_tables_added: Data Vault link_equipment_po can now be built from OBJK+SER01. 5 tables intentionally empty (MCHB MVKE MSKA LQUA LTAP) — documented in known_decisions.
- **#8** (2026-04-14) — data_vault_layer_built: Data Vault complete. 33 incremental models (spec said 32 but listed 16 satellites so actual total is 33). Hubs insert-only. Satellites use hashdiff for SCD2 change detection. Fixed link_equipment_gr to hash MBLNR+MJAHR via SERI->MKPF join so hk_material_document matches hub definition. Ready for marts layer.
- **#12** (2026-04-14) — dq_moved_to_dbt_tests: DQ = dbt tests (pass/fail at build time). Knowledge = business state (queryable views). Never mix these concerns.
- **#17** (2026-04-15) **[NEVER_REPEAT]** — link_po_item_unit_of_work: Every PO-item grain satellite must hang off link_po_item. Never create a satellite parented on a hash key that does not exist as a PK in a hub or link.
- **#19** (2026-04-16) — end_of_task_seeds_scanner_outputs: end_of_task.py is self-contained: scan -> seed scanner outputs -> export Parquet. No manual dbt seed needed after running it.
- **#47** (2026-04-17) **[NEVER_REPEAT]** — archive_moves_sql_files_to_isolated_folder: Archive = move not delete. Active dbt/models stays uncluttered. dbt_project.yml archive: +enabled: false is the single exclusion point. The scanner handles the catalog cleanup automatically.
- **#81** (2026-04-26) — schema_discovery_renders_compact_structural_facts_only: Per-analyzer rendering policy is a legitimate optimization surface when an analyzer's result_json contains both structural facts and supporting evidence prose. Future analyzers with similar shapes (verbose evidence + compact structural conclusions) should consider compact rendering at design time, not retrofit. The structural facts must be sufficient for the LLM consumption directive that will read them.
- **#96** (2026-05-05) — rule3_enforcement_checker_plus_two_mart_refactors: RULE 3 now has a checker. Two of three violators refactored to vault-sourced patterns matching fact_purchase_orders / fact_invoices conventions. The third is allowlisted with a clear fix path (KI-121: build sat_invoice_item + zmm_approval_log staging+vault, then refactor). Future model changes that introduce new RULE 3 violations will hard-fail end_of_task.py.
- **#106** (2026-05-12) — phase_gamma_pre_public_release_drop_decorative_seeds: Pre-public-release cleanup pass 1 of main_seeds complete. The drop list started at 7 candidates and narrowed to 3 after sharper auditing; the reversal kept 4 seeds that initially looked decorative but actually drive UI behavior. Remaining hand-maintained seeds (procurement_rules, org_structure, abap_logic_catalog, z_tables_catalog, zmm_approval_status, zmm_reason_codes, data_contracts, data_vault_design) all have either load-bearing readers or auto-sync mechanisms. Followup: data_vault_design is currently stale by 8 entities (hub_zmm_approval, sat_zmm_approval, sat_invoice_item, hub/sat/sat_text_movement_type, sat_vendor_business, sat_material_business) because end_of_task.py was bypassed on recent phase commits; the sync function in scan_dbt_models.py:sync_vault_design_seed exists and runs on end_of_task.
- **#107** (2026-05-12) **[NEVER_REPEAT]** — order_size_does_not_affect_lead_time_days: No meaningful effect — vendor capacity is not the bottleneck at these volumes. Splitting large orders would not gain lead-time reduction. Migrated from decommissioned signal_relationships #4 (2026-05-12, decision #106). Re-test only if HT volume mix changes significantly (e.g., 10x scale-up).
- **#108** (2026-05-12) **[NEVER_REPEAT]** — vendor_country_appears_not_to_affect_defect_rate_but_query_was_broken: DO NOT use obt_procurement_overview alone for vendor attribution of equipment outcomes — the material_number join is many-to-many across vendors. Correct path: traverse link_equipment_gr -> link_gr_po -> link_po_vendor in the vault, or build a fact_equipment_with_vendor mart that follows that chain. Migrated from decommissioned signal_relationships #6.
- **#109** (2026-05-12) **[NEVER_REPEAT]** — no_monthly_seasonality_in_synthetic_procurement_volume: Null result expected — generate_sap_sample_data.py creates uniform demand across the calendar. DO NOT re-run this analysis on synthetic data; the answer is fixed by construction. Re-test only on real HT production data, especially if explicit seasonal campaigns (holiday promos, fiscal-year close) are introduced. Migrated from decommissioned signal_relationships #7.
- **#112** (2026-06-25) — path3_sd_fi_infra_build: SD + FI are now first-class in the warehouse. Margin term cost side v1 = revenue - procurement cost; returns (MSEG 161/122 are DMBTR=0, unvalued) and warranty (ZHT_WARRANTY_LOG catalog-only, no data) deferred. Reference: scripts/generate_sd_billing.py, scripts/generate_fi_shadows.py, dbt/models/vault/link_sales_order_equipment.sql.
- **#114** (2026-06-25) **[NEVER_REPEAT]** — fi_link_join_key_must_be_single_table_per_side: A join key expression must depend on only one table per side or DuckDB falls back to nested-loop. For AWKEY-style concatenated keys, derive each side independently (the other two FI links join on r.BELNR\|\|r.GJAHR / m.MBLNR\|\|m.MJAHR and built in <0.5s). Diagnose slow queries by reading the plan BEFORE assuming locks/contention. Do not leave heavy read-only DuckDB queries running - one stray handle blocks every dbt write.
- **#117** (2026-06-27) **[NEVER_REPEAT]** — term_eda_requires_full_scope_domain_eda: Prefer minimal scope. A wide term incurs quadratic grain_relationship EDA cost before Term EDA can even start.

## Related Domain Relationships (0)

_(none)_

## Open Issues (6)

- **#5** [open/medium] Vendor-equipment attribution requires vault traceability — Q4 discovery showed obt_procurement_overview alone cannot attribute equipment outcomes to vendors because multiple vendors share material numbers (join fan-out). Need a fact_equipment_with_vendor model built from link_equipment_gr -> link_gr_po -> link_po_vendor. Backlog for next…
- **#66** [open/low] s2t_mapping undocumented in schema.yml — schema.yml has no entry for s2t_mapping. The 14-column shape (id, business_term_id, business_term_name, source_table, source_field, source_description, target_model, target_column, transformation_logic_plain, transformation_logic_sql, join_description, filter_description, notes, …
- **#69** [open/medium] Post-demo work — term-analysis attestation completeness + architectural hygiene — Series of gaps documented in the post-demo roadmap. Includes: TAR-NNNNN citation format not taught to the term-analysis runner; no `tar_consumed` attestation field; deterministic-analyzer directives missing (date / segmentation / grain_relationship / performance_baseline / schema…
- **#70** [open/low] Grain_relationship analyzer discovery function subsumed by schema_discovery — schema_discovery (Stage F) includes sum-match evidence in its relationship_shapes output for 1:N header-detail classifications, fully subsuming grain_relationship's discovery function. Grain_relationship remains potentially useful for term-scoped verification (verify relationship…
- **#95** [open/medium] LIMIT-50 generic DAR cap silently starves entire scope tables when batches run on a subset of scope — Discovered 2026-04-26 during step 4 of Theme 1 (post-C1 + post-#93 verification). For BG027's scope (equi/mseg/mkpf/objk/mard), 12 in-scope EDA DARs sit at ranks 51-62 and never reach the bundle. 11 of the 12 are mard rows; the 12th is objk performance_baseline. All 12 are status…
- **#122** [open/medium] dim_equipment uniqueness fails - link_equipment_material fan-out (1 device -> multiple materials) — link_equipment_material has 75236 rows for 45000 distinct equipment; 30236 devices (~67%) link to >1 material (e.g. CPE-00000330, an ONT, links to both CPE-ONT-003 and CPE-RTR-001). This fans dim_equipment out to 75236 rows, breaking unique_dim_equipment_equipment_number and uniq…

## DO NOT (Anti-patterns)

- **#12** (2026-04-14) — dq_moved_to_dbt_tests: DQ = dbt tests (pass/fail at build time). Knowledge = business state (queryable views). Never mix these concerns.
- **#17** (2026-04-15) **[NEVER_REPEAT]** — link_po_item_unit_of_work: Every PO-item grain satellite must hang off link_po_item. Never create a satellite parented on a hash key that does not exist as a PK in a hub or link.
- **#47** (2026-04-17) **[NEVER_REPEAT]** — archive_moves_sql_files_to_isolated_folder: Archive = move not delete. Active dbt/models stays uncluttered. dbt_project.yml archive: +enabled: false is the single exclusion point. The scanner handles the catalog cleanup automatically.
- **#107** (2026-05-12) **[NEVER_REPEAT]** — order_size_does_not_affect_lead_time_days: No meaningful effect — vendor capacity is not the bottleneck at these volumes. Splitting large orders would not gain lead-time reduction. Migrated from decommissioned signal_relationships #4 (2026-05-12, decision #106). Re-test only if HT volume mix changes significantly (e.g., 10x scale-up).
- **#108** (2026-05-12) **[NEVER_REPEAT]** — vendor_country_appears_not_to_affect_defect_rate_but_query_was_broken: DO NOT use obt_procurement_overview alone for vendor attribution of equipment outcomes — the material_number join is many-to-many across vendors. Correct path: traverse link_equipment_gr -> link_gr_po -> link_po_vendor in the vault, or build a fact_equipment_with_vendor mart that follows that chain. Migrated from decommissioned signal_relationships #6.
- **#109** (2026-05-12) **[NEVER_REPEAT]** — no_monthly_seasonality_in_synthetic_procurement_volume: Null result expected — generate_sap_sample_data.py creates uniform demand across the calendar. DO NOT re-run this analysis on synthetic data; the answer is fixed by construction. Re-test only on real HT production data, especially if explicit seasonal campaigns (holiday promos, fiscal-year close) are introduced. Migrated from decommissioned signal_relationships #7.
- **#114** (2026-06-25) **[NEVER_REPEAT]** — fi_link_join_key_must_be_single_table_per_side: A join key expression must depend on only one table per side or DuckDB falls back to nested-loop. For AWKEY-style concatenated keys, derive each side independently (the other two FI links join on r.BELNR\|\|r.GJAHR / m.MBLNR\|\|m.MJAHR and built in <0.5s). Diagnose slow queries by reading the plan BEFORE assuming locks/contention. Do not leave heavy read-only DuckDB queries running - one stray handle blocks every dbt write.
- **#117** (2026-06-27) **[NEVER_REPEAT]** — term_eda_requires_full_scope_domain_eda: Prefer minimal scope. A wide term incurs quadratic grain_relationship EDA cost before Term EDA can even start.
