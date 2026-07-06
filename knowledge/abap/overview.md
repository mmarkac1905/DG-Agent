# ABAP Custom Code Catalog

_Last generated: 2026-07-06 19:02:51_

Custom ABAP programs, user exits, BAdIs, and enhancements in HT's SAP system.
In a real engagement, this is auto-populated by Claude scanning exported ABAP source code.

## Programs by Risk Level

### CRITICAL (2)

- **ABAP009** `ZSD_CPE_PROVISIONING_TRIGGER` (RFC Function Module) — Sends provisioning request to OSS system when CPE deployment (mvt 201) is posted. Contains customer location and service
  - Reads: `MSEG;EQUI;ZHT_CUST_INSTALL` | Writes: ``
  - Rule: When movement type 201 is posted: read equipment serial from MSEG, look up customer installation details from ZHT_CUST_INSTALL (address, service type, bandwidth), call RFC to OSS provisioning system w

- **ABAP010** `ZSD_PROVISION_RETRY` (Report (background)) — Retries failed provisioning requests from ZHT_PROVISION_LOG. Runs every 15 minutes.
  - Reads: `ZHT_PROVISION_LOG` | Writes: `ZHT_PROVISION_LOG`
  - Rule: Reads entries with status = 'FAILED' and retry_count < 5. Calls ZHT_RFC_PROVISION again. On success: status = 'COMPLETED'. On failure: increment retry_count. After 5 failures: status = 'MANUAL_REVIEW'

### HIGH (4)

- **ABAP002** `ZMM_AUTO_EQUI_CREATE` (User Exit (MIGO)) — Automatically creates Equipment master record when CPE is received via GR (movement type 101). Links serial number to ma
  - Reads: `MSEG;MARA;MARC;LFA1` | Writes: `EQUI;EQBS`
  - Rule: On every GR posting with movement type 101 and serial number profile HT01: create EQUI record with status AVLB (available), link to material number, set manufacturer from vendor master LFA1.NAME1, set

- **ABAP003** `ZMM_CPE_STATUS_UPDATE` (BAdI (ME_PROCESS_PO_CUST)) — Updates CPE equipment status when goods movement is posted. Maps movement types to lifecycle states.
  - Reads: `MSEG;EQUI;EQBS` | Writes: `EQUI;EQBS`
  - Rule: Movement type 101 → status AVLB (available). Movement type 201 → status INST (installed). Movement type 161 → status RET (returned). Movement type 122 → status DLFL (defective). Updates both current s

- **ABAP012** `ZMM_INVOICE_THREE_WAY_MATCH` (Enhancement (MIRO)) — Enforces three-way matching for invoice verification: PO price vs invoice price vs GR quantity. Blocks invoice if discre
  - Reads: `RBKP;RSEG;EKPO;EKBE` | Writes: ``
  - Rule: Three-way match: (1) Invoice qty must match GR qty (EKBE) within 2%. (2) Invoice unit price must match PO price (EKPO.NETPR) within 3%. (3) Total invoice amount must match PO value * GR ratio within 5

- **ABAP015** `ZMM_BATCH_GR_UPLOAD` (Report (manual)) — Bulk goods receipt posting from Excel upload. Used when large shipments arrive (500+ devices) and manual MIGO entry is i
  - Reads: `Excel upload;EKPO;MARC` | Writes: `MKPF;MSEG;EQUI;EQBS`
  - Rule: Reads Excel file with columns: PO number, item, material, quantity, serial numbers (comma-separated). Validates each row against EKPO. Posts GR (101) for each row. Creates EQUI records for each serial

### MEDIUM (6)

- **ABAP001** `ZMM_CPE_SERIAL_CHECK` (User Exit (MIGO)) — Validates CPE serial number format at goods receipt. Rejects posting if serial doesn't match vendor-specific pattern.
  - Reads: `MSEG;EQUI;ZHT_SERIAL_FORMAT` | Writes: `EQUI`
  - Rule: Serial number must match vendor-specific pattern: Huawei = SN-HW-{type}-{5digits}, ZTE = SN-ZTE-{6digits}, Nokia = SN-NK-{model}-{4digits}. GR is rejected with error message if pattern invalid.

- **ABAP006** `ZMM_GR_QUANTITY_TOLERANCE` (User Exit (MIGO)) — Enforces goods receipt quantity tolerance. Rejects GR if received quantity exceeds PO quantity by more than 5%.
  - Reads: `MSEG;EKPO` | Writes: ``
  - Rule: GR quantity must be <= PO ordered quantity * 1.05. Over-deliveries beyond 5% are rejected with error. Partial deliveries are always allowed. Tolerance of 5% was set by Head of Logistics in 2022.

- **ABAP007** `ZMM_AUTO_PR_FROM_STOCK` (Report (MRP enhancement)) — Automatically creates purchase requisitions when CPE stock drops below reorder point. Runs as part of MRP planning run.
  - Reads: `MARD;MARC;EBAN` | Writes: `EBAN;EBKN`
  - Rule: For each material where MARD.LABST < reorder_point (from MARC or custom table ZHT_REORDER_POINTS): create EBAN with quantity = (target_stock - current_stock). Sets EBAN.ESTKZ = 'B' (MRP-generated). Co

- **ABAP011** `ZMM_WAREHOUSE_TRANSFER_RULES` (Enhancement (MIGO)) — Validates inter-plant transfers (movement type 301). Ensures receiving plant has storage capacity and material is author
  - Reads: `MARD;T001L;ZHT_PLANT_CAPACITY;ZHT_MATERIAL_AUTH` | Writes: ``
  - Rule: Transfer blocked if: (1) receiving storage location current stock + transfer qty > ZHT_PLANT_CAPACITY max_capacity, or (2) material not in ZHT_MATERIAL_AUTH for target plant. Zagreb central warehouse

- **ABAP013** `ZMM_CPE_WARRANTY_TRACK` (User Exit (MIGO)) — Records warranty start date when CPE is received (mvt 101). Warranty period looked up from material master custom field
  - Reads: `MSEG;MARA;EQUI` | Writes: `ZHT_WARRANTY_LOG`
  - Rule: On GR posting: read ZZ_WARRANTY_MONTHS from MARA (typically 24 for routers, 36 for ONTs, 12 for STBs). Calculate warranty_end = GR_date + warranty_months. Write to ZHT_WARRANTY_LOG with serial, materi

- **ABAP016** `ZFI_CPE_DEPRECIATION` (Report (monthly)) — Calculates monthly depreciation for deployed CPE assets. Straight-line over expected lifecycle from cpe_catalog.
  - Reads: `EQUI;EQBS;MARA;ZHT_CPE_LIFECYCLE` | Writes: `BKPF;BSEG`
  - Rule: For each EQUI with status INST (deployed): monthly_depreciation = purchase_price / lifecycle_months. Posts FI document debiting depreciation expense (account 6500100) and crediting accumulated depreci

### LOW (4)

- **ABAP004** `ZMM_DUAL_SOURCE_CHECK` (Enhancement (ME21N)) — Warns buyer if purchase order creates single-source dependency. Checks if this PO would push vendor concentration above
  - Reads: `EKKO;EKPO;LFA1;ZHT_VENDOR_SPEND` | Writes: ``
  - Rule: Warning message (non-blocking) if: total spend for this vendor + current PO value > 60% of total spend for this material group in current fiscal year. Reads from custom aggregation table ZHT_VENDOR_SP

- **ABAP005** `ZMM_REFRESH_SPEND_AGG` (Report (batch job)) — Nightly aggregation of vendor spend per material group per fiscal year. Feeds the dual-source check in ME21N.
  - Reads: `EKKO;EKPO;EKBE` | Writes: `ZHT_VENDOR_SPEND`
  - Rule: Aggregates EKPO.NETWR grouped by EKKO.LIFNR and EKPO.MATKL and fiscal year. Only counts PO items with at least one GR posting in EKBE (confirmed deliveries not just orders). Runs every night at 02:00.

- **ABAP008** `ZMM_VENDOR_EVAL_SCORE` (Report (periodic)) — Calculates vendor performance scores: on-time delivery rate and defect rate. Stores in custom table for dashboard consum
  - Reads: `EKKO;EKPO;EKET;EKBE;MKPF;MSEG` | Writes: `ZHT_VENDOR_SCORES`
  - Rule: For each vendor per quarter: (1) OTD = count(GR date <= EKET delivery date + 2 days) / count(all GR). (2) Defect rate = count(movement type 122 within 90 days of mvt 101) / count(mvt 101). (3) Overall

- **ABAP014** `ZMM_COST_CENTER_DERIVE` (BAdI (ME_PROCESS_PO_CUST)) — Auto-derives cost center on PO based on receiving plant. Saves buyer from manual entry.
  - Reads: `T001W;ZHT_PLANT_CC_MAP` | Writes: `EKKN`
  - Rule: Maps plant to default cost center: HT10 → CC-HT10-CPE, HT20 → CC-HT20-CPE, HT30 → CC-HT30-CPE, HT40 → CC-HT40-CPE. Buyer can override. If plant not in mapping table, leaves blank for manual entry.


## Z-Tables (Custom Tables)

| Table | Description | Maintained by | Rows | Referenced by |
| --- | --- | --- | --- | --- |
| `ZHT_SERIAL_FORMAT` | Vendor-specific serial number patterns for CPE validation | CPE Team | ~24 | ABAP001 |
| `ZHT_VENDOR_SPEND` | Aggregated vendor spend per material group per fiscal year | System (batch job) | ~~200 | ABAP004;ABAP005 |
| `ZHT_REORDER_POINTS` | CPE reorder points and target stock levels per material per | CPE Planning Team | ~40 | ABAP007 |
| `ZHT_VENDOR_SCORES` | Calculated vendor performance scores per quarter | System (batch job) | ~~120 | ABAP008 |
| `ZHT_CUST_INSTALL` | Customer CPE installation details for provisioning | Field technician (mobile app) | ~~35000 | ABAP009 |
| `ZHT_PROVISION_LOG` | Provisioning request log with status tracking | System | ~~40000 | ABAP009;ABAP010 |
| `ZHT_PLANT_CAPACITY` | Maximum storage capacity per storage location per material g | Warehouse Management | ~35 | ABAP011 |
| `ZHT_MATERIAL_AUTH` | Authorized materials per plant — controls what can be stored | CPE Planning Team | ~60 | ABAP011 |
| `ZHT_WARRANTY_LOG` | CPE warranty tracking with start and end dates | System + Quality team | ~~45000 | ABAP013 |
| `ZHT_PLANT_CC_MAP` | Plant to cost center mapping for automatic PO account assign | Controlling team | ~4 | ABAP014 |
| `ZHT_CPE_LIFECYCLE` | Expected lifecycle months per material type for depreciation | Finance + CPE team | ~10 | ABAP016 |

## Table Dependency Graph

Which ABAP programs read/write which tables:

| Table | Read by | Written by |
| --- | --- | --- |
| `BKPF` |  | ABAP016 |
| `BSEG` |  | ABAP016 |
| `EBAN` | ABAP007 | ABAP007 |
| `EBKN` |  | ABAP007 |
| `EKBE` | ABAP005, ABAP008, ABAP012 |  |
| `EKET` | ABAP008 |  |
| `EKKN` |  | ABAP014 |
| `EKKO` | ABAP004, ABAP005, ABAP008 |  |
| `EKPO` | ABAP004, ABAP005, ABAP006, ABAP008, ABAP012, ABAP015 |  |
| `EQBS` | ABAP003, ABAP016 | ABAP002, ABAP003, ABAP015 |
| `EQUI` | ABAP001, ABAP003, ABAP009, ABAP013, ABAP016 | ABAP001, ABAP002, ABAP003, ABAP015 |
| `Excel upload` | ABAP015 |  |
| `LFA1` | ABAP002, ABAP004 |  |
| `MARA` | ABAP002, ABAP013, ABAP016 |  |
| `MARC` | ABAP002, ABAP007, ABAP015 |  |
| `MARD` | ABAP007, ABAP011 |  |
| `MKPF` | ABAP008 | ABAP015 |
| `MSEG` | ABAP001, ABAP002, ABAP003, ABAP006, ABAP008, ABAP009, ABAP013 | ABAP015 |
| `RBKP` | ABAP012 |  |
| `RSEG` | ABAP012 |  |
| `T001L` | ABAP011 |  |
| `T001W` | ABAP014 |  |
| `ZHT_CPE_LIFECYCLE` | ABAP016 |  |
| `ZHT_CUST_INSTALL` | ABAP009 |  |
| `ZHT_MATERIAL_AUTH` | ABAP011 |  |
| `ZHT_PLANT_CAPACITY` | ABAP011 |  |
| `ZHT_PLANT_CC_MAP` | ABAP014 |  |
| `ZHT_PROVISION_LOG` | ABAP010 | ABAP010 |
| `ZHT_SERIAL_FORMAT` | ABAP001 |  |
| `ZHT_VENDOR_SCORES` |  | ABAP008 |
| `ZHT_VENDOR_SPEND` | ABAP004 | ABAP005 |
| `ZHT_WARRANTY_LOG` |  | ABAP013 |
