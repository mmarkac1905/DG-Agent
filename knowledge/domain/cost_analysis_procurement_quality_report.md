---
generated: 2026-04-17T00:46:32
findings_count: 38
qa_count: 4
business_terms: avg_vendor_lead_time, total_cost_of_ownership, goods_receipt_accuracy, vendor_concentration_risk, total_po_value
---

## CPE Procurement — Domain Snapshot

Helios Telecom runs a substantial CPE procurement operation captured across 4 core SAP tables (EKKO, EKPO, MSEG, MKPF) with comprehensive coverage of purchase orders, goods receipts, and inventory movements. The data supports 11 approved business metrics spanning procurement, quality, inventory, and cost analysis domains.

### Data Volume & Coverage

- **Purchase Orders**: Validated header-line relationships across EKKO-EKPO with confirmed clean joins for aggregation
- **Goods Receipts**: 4 distinct movement types identified in MSEG, with movement type 101 appearing as primary goods receipt indicator
- **Period Coverage**: Analysis confirms monthly and quarterly aggregation feasibility with consistent data across periods
- **Vendor Population**: Multiple distinct vendors confirmed across LIFNR dimension for vendor-level metrics

### Data Quality Observations

- **NETWR Completeness**: Strong population of net values in EKPO line items, enabling reliable spend calculations
- **Date Integrity**: Clean BEDAT (PO dates) and BUDAT (posting dates) fields support time-based analysis
- **Join Relationships**: EKKO-EKPO relationship validates at 100% — no orphaned line items detected
- **Currency Handling**: Currency column availability needs verification for EUR conversion requirements

### Business Metric Readiness

**Validated Calculations**:
- Total Purchase Orders: Clean EBELN counting confirmed across EKKO
- Average Vendor Lead Time: BEDAT to BUDAT calculation path established via EKKO-MKPF join
- On-Time Delivery Rate: Quarterly aggregation logic validated with accuracy percentage calculations
- Goods Receipt Accuracy: Quantity matching logic (MSEG.MENGE vs EKPO.MENGE) confirmed operational

**Requires Extension**:
- CPE Defect Rate: Movement type 122 (returns) pattern needs validation
- CPE Lifecycle Status: Equipment status transitions require MSEG movement type mapping
- Days of Stock: Movement type 201 (goods issues) volume needs profiling

### Critical Findings

- **Vendor Accuracy Rates**: 10 lowest-performing vendors identified with <90% delivery accuracy, indicating data quality issues or operational problems requiring investigation
- **High-Value Transactions**: 5 PO lines >50,000 EUR validated for reasonableness — no obvious data anomalies detected
- **Statistical Significance**: Minimum volume thresholds (10+ receipts) applied to vendor metrics ensure reliable calculations

### Data Gaps & Conflicts

Two areas require immediate attention:
1. **Currency Standardization**: WAERS field availability unclear — impacts multi-currency procurement analysis
2. **Movement Type Coverage**: Only 4 movement types confirmed — full CPE lifecycle requires validation of return (122) and issue (201) patterns


## Recommendations

- Validate currency field (WAERS) availability in EKPO and implement EUR conversion logic for accurate spend calculations
- Profile MSEG movement types 122 and 201 to confirm CPE return and deployment tracking capabilities
- Investigate the 10 vendors with <90% delivery accuracy from AF033 findings to determine if data quality or operational issues
- Establish minimum volume thresholds for all vendor-level metrics to ensure statistical reliability
- Run complete date range analysis on MKPF.BUDAT to confirm quarterly reporting coverage
- Validate EKBE table as primary goods receipt source versus MSEG-MKPF join for lead time calculations


**Report confidence:** HIGH