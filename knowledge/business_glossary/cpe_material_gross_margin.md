# Business Term: CPE Net Margin per Material per Period

_Last generated: 2026-06-25 22:40:51_

## Definition

For each CPE material and calendar month, net margin = total billed revenue (SD billing documents, VBRK/VBRP) minus total procurement cost (vendor-invoiced amount from MM-IV) minus returns cost (valuation of customer and vendor returns) minus warranty cost (ZMM_CPE_WARRANTY_TRACK accruals), expressed in EUR. Also reports margin_pct = net_margin / revenue. Answers which CPE devices actually generate profit once cost of goods, returns, and warranty are netted against what we bill the customer.

- **ID:** `BG030`
- **Owner:** Finance
- **Approved by:** 
- **Status:** `draft`
- **Unit:** EUR + Percent
- **Grain:** material x month
- **Domain:** procurement_finance
- **Related terms:** [BG028](BG028.md)

**Notes:** Path 3 walkthrough term (real profitability). Net margin chosen over gross: cost side nets procurement cost + returns cost + warranty cost against SD revenue. Per-material-per-period grain (material x month) so margin trend is visible over time. PREREQUISITE: SD revenue tables (VBAK/VBAP/VBRK/VBRP/KNA1) and invoice-side FI postings (BLART=RE) do not yet exist - infra build precedes Stage A. Output columns (intended): material_number, month, revenue_eur, procurement_cost_eur, returns_cost_eur, warranty_cost_eur, net_margin_eur, margin_pct.

## Source-to-Target Mapping

_(no S2T mapping defined yet)_

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

_(lineage will be documented when dbt models are built)_

## Validation Status

DRAFT — awaiting business owner approval

## Related Decisions (1)

- **#112** (2026-06-25) — path3_sd_fi_infra_build: SD + FI are now first-class in the warehouse. Margin term cost side v1 = revenue - procurement cost; returns (MSEG 161/122 are DMBTR=0, unvalued) and warranty (ZHT_WARRANTY_LOG catalog-only, no data) deferred. Reference: scripts/generate_sd_billing.py, scripts/generate_fi_shadows.py, dbt/models/vault/link_sales_order_equipment.sql.
