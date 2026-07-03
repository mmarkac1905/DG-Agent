# CPE Procurement Analytics — Knowledge Wiki

_Last generated: 2026-07-04 01:42:32_

Auto-generated from `dbt/seeds/` — do not edit files in this directory by hand.

## System Snapshot

- **Latest version (decisions seed):** `wire_dbt_compile_eot`
- **Total decisions:** 125
- **Total domain relationships:** 0
- **Total known issues:** 126 (38 open)
- **Data products tracked:** 0
- **Data Vault entities designed:** 60 (15 hubs · 17 links · 28 satellites)
- **Business glossary terms:** 33 (25 approved · 3 draft)
- **ABAP custom programs:** 16 documented (2 critical · 4 high risk) · **Z-tables:** 11
- **Data contracts:** 21 (15 daily · 6 weekly)
- **dbt models scanned:** 135 (knowledge: 6, marts: 18, obt: 6, staging: 45, vault: 60)
- **Column lineage tracked:** 1356 columns across all layers

## Data Products (Analytical Use Cases)


## Business Glossary

### Approved Terms
- [Average Vendor Lead Time](business_glossary/avg_vendor_lead_time.md) — procurement · vendor × month
- [On-Time Delivery Rate](business_glossary/on_time_delivery_rate.md) — procurement · vendor × quarter
- [CPE Defect Rate](business_glossary/cpe_defect_rate.md) — quality · material × vendor × quarter
- [Inventory Turnover Ratio](business_glossary/inventory_turnover_ratio.md) — inventory · material_group × quarter
- [Days of Stock (DOS)](business_glossary/days_of_stock.md) — inventory · material × plant
- [CPE Lifecycle Status](business_glossary/cpe_lifecycle_status.md) — equipment · equipment (serial number)
- [PO Cycle Time](business_glossary/purchase_order_cycle_time.md) — procurement · purchasing_group × month
- [Vendor Concentration Risk](business_glossary/vendor_concentration_risk.md) — procurement · vendor × quarter
- [Total Purchase Orders](business_glossary/total_purchase_orders.md) — procurement · period
- [Total PO Value](business_glossary/total_po_value.md) — procurement · period
- [Delivery Status Distribution](business_glossary/delivery_status_distribution.md) — procurement · po_item
- [Vendor Spend Share](business_glossary/vendor_spend_share.md) — procurement · vendor x quarter
- [Vendor Fulfillment Rate](business_glossary/vendor_fulfillment_rate.md) — procurement · vendor
- [Vendor Performance Grade](business_glossary/vendor_performance_grade.md) — procurement · vendor
- [Total CPE Devices](business_glossary/total_cpe_devices.md) — equipment · total
- [CPE Deployment Rate](business_glossary/cpe_deployment_rate.md) — equipment · equipment_category
- [CPE Return Rate](business_glossary/cpe_return_rate.md) — equipment · equipment_category
- [Total Stock Units](business_glossary/total_stock_units.md) — inventory · total or per material per plant
- [Zero-Stock Locations](business_glossary/zero_stock_locations.md) — inventory · total
- [Blocked Stock](business_glossary/blocked_stock.md) — inventory · total or per material
- [Total Goods Movements](business_glossary/total_goods_movements.md) — inventory · period
- [Deployment to Return Ratio](business_glossary/deployment_return_ratio.md) — equipment · equipment_category
- [PO Volume by Month](business_glossary/po_volume_by_month.md) — procurement · month
- [PO-Invoice Three-Way Match Variance](business_glossary/po_invoice_three_way_match_variance.md) — procurement_finance · vendor x month
- [CPE Contribution Margin by Service Plan & Tenure](business_glossary/cpe_contribution_margin_by_segment.md) — procurement_finance · service_plan x tenure_band x month

### Draft Terms (awaiting approval)
- [Total Cost of Ownership (TCO)](business_glossary/total_cost_of_ownership.md) — cost_analysis · draft
- [Goods Receipt Accuracy](business_glossary/goods_receipt_accuracy.md) — quality · draft
- [Olist On-Time Delivery Rate](business_glossary/olist_on_time_delivery_rate.md) — ecommerce_sales · draft

## SAP Tables

- [purchase_orders](sap_tables/purchase_orders.md)
- [goods_receipts](sap_tables/goods_receipts.md)
- [materials](sap_tables/materials.md)
- [vendors](sap_tables/vendors.md)
- [inventory](sap_tables/inventory.md)
- [equipment](sap_tables/equipment.md)
- [invoices](sap_tables/invoices.md)
- [accounting](sap_tables/accounting.md)
- [purchase_requisitions](sap_tables/purchase_requisitions.md)
- [org_structure](sap_tables/org_structure.md)

## Domain Concepts

- [procure_to_deploy](domain/procure_to_deploy.md)
- [cpe_lifecycle](domain/cpe_lifecycle.md)
- [provisioning](domain/provisioning.md)
- [goods_receipt](domain/goods_receipt.md)

## Data Vault Design

- [hub_design](data_vault/hub_design.md)
- [link_design](data_vault/link_design.md)
- [satellite_design](data_vault/satellite_design.md)
- [naming_conventions](data_vault/naming_conventions.md)

## Infrastructure

- [duckdb](infrastructure/duckdb.md)
- [dbt_project](infrastructure/dbt_project.md)
- [pipeline](infrastructure/pipeline.md)
- [dashboard](infrastructure/dashboard.md)

## ABAP Custom Code

- [overview](abap/overview.md) — all custom programs, Z-tables, dependency graph
- Programs: 16 total (2 critical, 4 high risk)
- Z-Tables: 11 custom tables documented

## Meta Pages

- [anti_patterns](anti_patterns.md) — DO NOT list, scannable in 30s
- [reminders](reminders.md) — dated open issues, overdue flagged

## Usage

1. Before ANY building, change, or suggestion: read `index.md`, the relevant data product page, and `anti_patterns.md`.
2. If the work touches SAP tables, read the relevant `sap_tables/` page.
3. If the work touches Data Vault design, read the relevant `data_vault/` page.
4. If implementing a business metric, read the `business_glossary/` page — it has the approved definition, S2T mapping, transformation logic, and profiling config.
5. Never duplicate content — edit the seed CSV and rerun `python scripts/build_knowledge_wiki.py`.
