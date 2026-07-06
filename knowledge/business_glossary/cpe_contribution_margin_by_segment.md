# Business Term: CPE Contribution Margin by Service Plan & Tenure

_Last generated: 2026-07-07 01:07:16_

## Definition

Per service plan type and customer tenure band (per month), contribution margin = total billed service revenue (SD billing VBRK/VBRP, non-cancelled) minus amortized CPE device cost (device unit purchase cost spread straight-line over 24 months, charged only while a deployed device is within its 24-month amortization window), in EUR. Segments by service_plan (fiber/TV/cable/business) and tenure_band (months since customer acquisition: 0-12, 12-24, 24-48, 48+). Reveals where the CPE-giveaway model pays off - contribution margin turns strongly positive after the 24-month device payoff for retained customers.

- **ID:** `BG030`
- **Owner:** Finance
- **Approved by:** 
- **Status:** `approved`
- **Unit:** EUR
- **Grain:** service_plan x tenure_band x month
- **Domain:** procurement_finance
- **Related terms:** [BG028](BG028.md)

**Notes:** Customer-centric re-spec 2026-06-28 (pivoted from material gross margin per decision #118): telco CPE economics are retention-driven - the device giveaway pays off only if customers stay past the 24-month amortization. Tenure bands 0-12/12-24/24-48/48+ months since acquisition (KNA1.ERDAT / first-bill). Segment dims: service_plan (SVC-FIB/TV/CBL/BIZ); secondary customer_type (KTOKD residential/business) + city (ORT01, 12 cities). SCOPE EXPANSION expected: adds KNA1 (customer master), VBAK/VBAP (sales order -> service plan + customer link), EQUI (device deployment date for amortization window + tenure) to the prior revenue/cost/material/returns tables. Output columns (intended): service_plan, tenure_band, month, revenue_eur, amortized_device_cost_eur, contribution_margin_eur. \|\| DERIVATION RULES (locked 2026-06-28): (1) tenure_anchor = first_bill_date = MIN(VBRK.FKDAT) per customer (NOT KNA1.ERDAT). (2) deployment_date = COALESCE(EQUI.INBDT, first_bill_date) [INBDT 40% null; MSEG BWART-201 fallback NOT buildable - 201 deployment movements carry no serial linkage in this dataset, only 101/GR do - see issue #128; first-bill proxy aligns cost recognition with revenue start]. (3) device_unit_cost = EKPO.NETPR (PO net price, NOT RSEG.WRBTR). (4) amortized_device_cost = NETPR/24 straight-line for 24 months from deployment_date. (5) per-device link = VBAP.SERNR -> SERI -> EQUI. RETURNS LEG DROPPED 2026-06-28 (dec #120): vendor returns (BWART 161/122) are unvalued (DMBTR=0) and no customer returns exist in data; MSEG/MKPF/RBKP/RSEG now unused by the metric. INBDT->first-bill rule confirmed by analyst (resolves Term EDA escalation).

## Source-to-Target Mapping

### Source Tables

| Table | Field | Description |
| --- | --- | --- |
| VBRK | FKSTO | Billing document header — provides FKSTO (cancellation flag; filter FKSTO='' to exclude cancelled documents per business_filter_description), FKDAT (billing date used as the basis for MIN(FKDAT) per customer to establish the tenure anchor), KUNAG (sold-to party linking to KNA1 for secondary segment attributes), and WAERK (currency). Revenue grain is monthly by billing date. |
| VBRP | NETWR | Billing document line — provides NETWR (net billed service revenue in EUR, the primary revenue measure), MATNR (service-plan material for CPE filter and service_plan segmentation), MATKL (material group for CPE-% filter), AUBEL/AUPOS (reference back to originating sales order, enabling join to VBAK/VBAP for service plan and device link). Joins to VBRK on VBELN. |
| VBAK | VBELN | Sales order header — provides KUNNR (customer link to KNA1 for secondary segments KTOKD and ORT01) and AUART/SPART/VTWEG for service plan classification. Needed because VBRP.AUBEL references the sales order number; VBAK bridges the billing line to the customer master. |
| VBAP | MATNR | Sales order line item — provides MATKL (material group for CPE-% filter), MATNR (service-plan material number driving fiber/TV/cable/business segmentation), and SERNR (serial number of the deployed CPE device). VBAP.SERNR is the entry point into the per-device amortization chain: VBAP.SERNR → SERI.SERNR → SERI.EQUNR → EQUI.INBDT. |
| KNA1 | KTOKD | Customer master — retained in scope for the secondary segment dimensions only: KTOKD (customer account group: residential/business) and ORT01 (city, 12-city secondary dimension). Tenure is no longer derived from KNA1.ERDAT; instead tenure = months since MIN(VBRK.FKDAT) per KUNAG. |
| EKKO | EBELN | Purchase order header — provides EBELN (PO number) and WAERS (currency) as the header anchor for the device unit cost chain (EKKO → EKPO). Required to filter CPE procurement documents and carry PO-level attributes to the line cost calculation. |
| EKPO | NETPR | Purchase order line — provides NETPR (net price per unit, the authoritative device unit purchase cost for straight-line amortization over 24 months per analyst decision #3) and MATKL (CPE material group filter). NETPR is the sole cost numerator in the amortization formula: amortized_monthly_cost = NETPR / 24. |
| RBKP | BELNR | Vendor invoice header — provides BELNR (invoice document number), BUDAT (invoice posting date), and LIFNR (vendor); needed to identify posted vendor invoices per the business_filter_description ('posted vendor invoices only'). Also provides the header-level posted status used to filter out unposted or parked invoices. |
| RSEG | WRBTR | Vendor invoice line — provides EBELN/EBELP (links to PO line, confirming which PO line items have been invoiced and are therefore 'posted') and MATNR (for CPE material filter at invoice line level). Note: per analyst decision #3, RSEG.WRBTR is NOT used as the device cost basis; EKPO.NETPR is used instead. RSEG remains in scope to identify which PO lines have confirmed posted invoices. |
| MARA | MATKL | Material master — provides MATKL (material group for 'MATKL LIKE CPE-%' filter) and MTART (material type) to restrict scope to CPE materials across all procurement, billing, and movement legs. Serves as the shared CPE-filter anchor across the cost, revenue, and returns sub-queries. |
| EQUI | INBDT | Equipment master — provides INBDT (date put into service / installation date), which is the deployment start date for the 24-month straight-line amortization window per analyst decision #2. A device is within its amortization window in month M if INBDT <= M < INBDT + 24 months. Joins to SERI via EQUNR. |
| SERI | EQUNR | Serial number record — provides the per-unit link from a deployed CPE device's serial number (SERNR, joinable from VBAP.SERNR) to the equipment master (EQUNR → EQUI.INBDT). Per analyst decision #4, SERI is used exclusively in the chain VBAP.SERNR → SERI.SERNR → SERI.EQUNR → EQUI.INBDT. The prior SERI → MSEG join (MBLNR/ZEILE) is removed because cardinality evidence showed catastrophic_fanout (DAR-00539) or no_signal (DAR-00579), making it an invalid per-device join key. |
| MSEG | BWART | Material document line — retained in scope ONLY for returns cost calculation per analyst decision #4: BWART (movement type; filter to 161/122 for return movements), DMBTR (amount in local currency for returns valuation), and MATNR (CPE material filter). Returns cost is aggregated at material/movement level (not joined per-device to SERI). Joins to MKPF for posting date. |
| MKPF | BUDAT | Material document header — provides BUDAT (goods movement posting date) for the returns leg; required as the header for MSEG to get the authoritative posting date for return events. Used solely in the returns cost aggregation sub-query (MSEG BWART 161/122), not in the per-device amortization path. |

### Transformation (plain language)

1. Carries the cancellation status flag directly from SAP field FKSTO on the VBRK billing document header, flowing through staging, vault, and mart layers unchanged.
2. Carries the net billed service revenue in EUR directly from SAP field VBRP.NETWR through staging, vault, and mart layers unchanged as the primary revenue measure underpinning contribution margin calculations by segment.
3. Carries the sales order number directly from VBAK.VBELN, flowing unchanged through staging, vault, and mart layers to link each billing line to its originating sales order header and associated customer, service plan, and segment attributes.
4. Carries the sales order line item's material number directly from VBAP.MATNR, flowing unchanged through staging, vault, and mart layers to drive fiber, TV, cable, and business segment classification for CPE contribution margin reporting.
5. This column carries the customer account group classification (residential or business) flowing through unchanged from SAP field KNA1.KTOKD, used exclusively to support secondary segment dimension filtering within the contribution margin by segment reporting structure.
6. Carries the purchase order number directly from SAP field EKKO.EBELN, flowing through staging, vault, and mart layers unchanged as the header anchor identifier for CPE contribution margin calculations by segment.
7. Carries the net price per unit sourced directly from SAP field NETPR (EKPO), flowing through staging, vault, and mart layers unchanged as the authoritative device unit purchase cost used in contribution margin by segment calculations.
8. This column carries the vendor invoice document number (BELNR) from RBKP unchanged through all pipeline layers into the contribution margin by segment fact table.
9. This column carries the vendor invoice line amount directly from SAP field RSEG.WRBTR, flowing through staging, vault, and mart layers unchanged to confirm which purchase order lines have posted invoices linked to CPE materials.
10. Carries the material group value directly from MARA.MATKL, flowing through staging, vault, and mart layers unchanged to anchor the CPE material scope filter across all cost, revenue, and returns segments.
11. This column carries the equipment installation date directly from SAP field INBDT (EQUI) — the date a device was put into service — flowing through staging, vault, and mart layers unchanged.
12. This column carries the equipment number directly from SAP field SERI.EQUNR, flowing unchanged through staging, vault, and mart layers as the per-unit link connecting a deployed CPE device's serial number to its equipment master record.
13. Carries the material movement type directly from SAP field MSEG.BWART, flowing through staging, vault, and mart layers unchanged to support filtering of return movements (161/122) in the returns cost calculation underlying contribution margin by segment.
14. This column carries the goods movement posting date directly from SAP field MKPF.BUDAT, flowing through staging, vault, and mart layers unchanged as the authoritative posting date for customer returns events used in the returns cost aggregation.

### SQL (from dbt models)

### Target Models

- `fact_contribution_margin_by_segment`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-06-16)

## Related Decisions (9)

- **#112** (2026-06-25) — path3_sd_fi_infra_build: SD + FI are now first-class in the warehouse. Margin term cost side v1 = revenue - procurement cost; returns (MSEG 161/122 are DMBTR=0, unvalued) and warranty (ZHT_WARRANTY_LOG catalog-only, no data) deferred. Reference: scripts/generate_sd_billing.py, scripts/generate_fi_shadows.py, dbt/models/vault/link_sales_order_equipment.sql.
- **#115** (2026-06-27) **[NEVER_REPEAT]** — pin_current_sonnet_not_dated_model_id: Dated model ids (claude-sonnet-4-20250514) get retired and 404 the whole pipeline. Pin the rolling alias claude-sonnet-4-6; better, centralize the id in one config so a retirement is a 1-line fix.
- **#116** (2026-06-27) **[NEVER_REPEAT]** — validator_enum_must_match_prompt_taxonomy: resolves_in enum is maintained in two places (prompt + validator) and drifted. Keep in sync; ideally derive both from one source.
- **#117** (2026-06-27) **[NEVER_REPEAT]** — term_eda_requires_full_scope_domain_eda: Prefer minimal scope. A wide term incurs quadratic grain_relationship EDA cost before Term EDA can even start.
- **#118** (2026-06-28) **[NEVER_REPEAT]** — cpe_margin_is_customer_centric_not_material_centric: CPE profitability is a customer/segment + retention question, not a per-material one. Amortize device capex straight-line over the 24-month giveaway period and match to deployed devices.
- **#119** (2026-06-28) **[NEVER_REPEAT]** — deployment_date_coalesce_inbdt_then_first_bill: When deployment movements are not serial-linked, first service-bill date is the most defensible deployment proxy (aligns cost recognition with revenue). Resolves the INBDT-null analyst_decision blocker.
- **#120** (2026-06-28) **[NEVER_REPEAT]** — drop_returns_leg_unvalued_in_data: Drop a margin component when the source movements carry no valuation. Like warranty earlier - synthetic data only values inbound (101) movements.
- **#121** (2026-06-28) **[NEVER_REPEAT]** — bg030_deployed_contribution_margin_mart: Customer-centric CPE contribution margin (service_plan x tenure_band x month) deployed end-to-end Stage 0->E. Always reconcile suspected magnitude bugs to source before declaring a defect.
- **#122** (2026-06-28) **[NEVER_REPEAT]** — bg030_mart_refactored_to_vault_layer: Marts must build on the vault layer; enforce the layering rule at GENERATION (prompt), not only at the commit gate.

## Open Issues (2)

- **#125** [open/medium] Stage A scope-history iter_num collides (propose+revise both iter 1) -> confirm can select wrong iteration — append_iteration_to_history writes only business_glossary.csv, but _propose_or_revise derives iter_num from the DB term row (scope_derivation_history_json), which stays {} until a dbt seed runs. So a revise after a propose (no reseed between) reads empty history and is numbered i…
- **#128** [open/low] Synthetic data: BWART=201 deployment movements lack serial-number linkage (only 101/GR linked) — All SERI (45000) and SER03 (2155) serial records point to BWART=101 goods-receipt movements; the 27000 BWART=201 deployment movements have ZERO serial/equipment linkage. So per-device deployment date cannot be derived from the 201 movement (forced the first-bill proxy for null EQ…
