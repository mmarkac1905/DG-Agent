# Business Term: PO-Invoice Three-Way Match Variance

_Last generated: 2026-07-03 21:52:59_

## Definition

For each vendor x month, percentage of PO lines where PO quantity, goods-receipt quantity, and invoice quantity all match within 5% tolerance. Also reports exception count and approved-exception count from ZMM_APPROVAL_LOG workflow. A line is matched when \|PO_QTY - GR_QTY\| / PO_QTY < 0.05 AND \|GR_QTY - INVOICE_QTY\| / GR_QTY < 0.05. Exceptions with APPR_STATUS='02' in ZMM_APPROVAL_LOG are approved exceptions. Unapproved exceptions are open variance risk.

- **ID:** `BG028`
- **Owner:** finance_ops
- **Approved by:** user
- **Status:** `approved`
- **Unit:** Percent + Count
- **Grain:** vendor x month
- **Domain:** procurement_finance
- **Related terms:** [BG008, BG010, BG013](BG008, BG010, BG013.md)

**Notes:** Demo-model expansion term. First glossary term whose scope exercises Layer A (zmm_approval_log is raw-only) alongside Layer B (ekko/ekpo/ekbe/rbkp/rseg/lfa1 all have dbt staging coverage). Three-way match tolerance is 5% per finance policy. Output columns: vendor_id, month, total_po_lines, matched_pct, exception_count, approved_exception_count, open_exception_count.

## Source-to-Target Mapping

### Source Tables (SAP)

| Table | Field | Description |
| --- | --- | --- |
| EKKO | LIFNR | Vendor account number |
| EKPO | MENGE | PO order quantity |
| EKBE | MENGE | GR quantity from PO history |
| RBKP | BELNR | Invoice document number |
| RSEG | MENGE | Invoice line quantity |
| LFA1 | NAME1 | Vendor name |
| ZMM_APPROVAL_LOG | APPR_STATUS | Approval workflow state |

### Transformation (plain language)

1. This column carries the vendor account number directly from the SAP EKKO.LIFNR field, flowing through unchanged to identify which vendor is associated with each three-way match variance record.
2. This column carries the purchase order quantity directly from the SAP field EKPO.MENGE, flowing through unchanged to support three-way matching variance analysis.
   - *Join:* EKPO.EBELN = EKKO.EBELN
3. This column carries the goods receipt quantity directly from the SAP EKBE.MENGE field, flowing through unchanged from the purchase order history.
   - *Join:* EKBE.EBELN+EBELP = EKPO.EBELN+EBELP
   - *Filter:* VGABE='1' for goods receipts
4. This column carries the invoice document number directly from SAP field RBKP.BELNR, flowing through unchanged to identify invoices in three-way match variance analysis.
   - *Join:* RBKP.EBELN = EKKO.EBELN
5. This column carries the invoice line quantity directly from SAP field RSEG.MENGE, flowing through unchanged from source to support three-way match variance analysis.
   - *Join:* RSEG.BELNR+GJAHR = RBKP.BELNR+GJAHR; RSEG.EBELN+EBELP = EKPO.EBELN+EBELP
6. This column carries the vendor name as a direct copy of the LFA1.NAME1 field from SAP, flowing through unchanged to support three-way match variance analysis.
   - *Join:* LFA1.LIFNR = EKKO.LIFNR
7. This column carries the approval workflow state directly from the SAP ZMM_APPROVAL_LOG.APPR_STATUS field, flowing through unchanged to track purchase order invoice three-way match variance approval status.
   - *Join:* ZMM_APPROVAL_LOG.BELNR+GJAHR = RBKP.BELNR+GJAHR
   - *Filter:* APPR_STATUS IN ('02','03','04') for all exceptions; '02' for approved

### SQL (from dbt models)

### Target Models

- `fact_three_way_match_variance`

## Data Profile

_(no profiling configured yet — will be computed after sample data generation)_

### Live Profile Stats

_(auto-computed after sample data is loaded — run `python scripts/profile_data.py`)_

## Data Vault Lineage

See dbt docs (`dbt docs generate && dbt docs serve`) for full DAG lineage.

Simplified path: **SAP source** → `staging` → `vault (hubs/links/sats)` → `marts (facts/dims)` → `obt (flattened)` → **this metric**

## Validation Status

APPROVED — Business owner approved definition (2026-04-21)

## Open Issues (1)

- **#94** [open/low] Scope-aware ordering for the term-analysis DAR loader (Option beta) - deferred follow-up to #93 — During #93 fix design, scope-aware ORDER BY was investigated as Option beta: replace LIMIT 50 ORDER BY executed_at_utc DESC with a scope-overlap-first prioritization (rows whose source_tables overlap the term scope rank above non-overlapping rows; recency within priority). Premis…
