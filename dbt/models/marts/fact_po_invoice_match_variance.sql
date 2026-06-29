{{ config(materialized='table') }}

/*
    Fact: PO ↔ GR ↔ Invoice 3-way match variance — grain = vendor × month.
    BAR-00002 — quantity match within 5% on PO/GR/Invoice; tolerance
    overrides tracked via ZMM_APPROVAL_LOG custom workflow.

    RULE 3 alignment (refactored 2026-05-05): vault-sourced. Replaces:
    - stg_sap__ekko + stg_sap__ekpo  -> sat_po_header + sat_po_item +
      link_po_material + link_po_vendor
    - stg_sap__ekbe (BWART='101' GR rows) -> sat_gr_item.movement_type='101'
      (data-equivalent: EKBE GR rows == MSEG GR rows, verified 2026-05-05)
    - stg_sap__rseg -> sat_invoice_item (newly built)
    - raw_sap.zmm_approval_log -> sat_zmm_approval (newly built via
      stg_sap__zmm_approval_log + hub_zmm_approval)
*/

WITH po_header AS (
    SELECT * FROM {{ ref('sat_po_header') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_po_header') }})
),

po_item AS (
    SELECT * FROM {{ ref('sat_po_item') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_po_item') }})
),

gr_item AS (
    SELECT * FROM {{ ref('sat_gr_item') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_gr_item') }})
),

inv_item AS (
    SELECT * FROM {{ ref('sat_invoice_item') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_invoice_item') }})
),

zmm_app AS (
    SELECT * FROM {{ ref('sat_zmm_approval') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_zmm_approval') }})
),

po_lines AS (
    SELECT
        pv.vendor_id,
        DATE_TRUNC('month', ph.po_date) AS month,
        lpm.purchase_order_number AS EBELN,
        lpm.po_item_number AS EBELP,
        pi.ordered_quantity AS po_qty
    FROM {{ ref('link_po_material') }} lpm
    JOIN po_header ph
        ON lpm.hk_purchase_order = ph.hk_purchase_order
    JOIN po_item pi
        ON lpm.hk_po_material = pi.hk_po_material
    JOIN {{ ref('link_po_vendor') }} pv
        ON lpm.hk_purchase_order = pv.hk_purchase_order
    WHERE pi.ordered_quantity > 0
),

gr_agg AS (
    SELECT
        gri.purchase_order_number AS EBELN,
        gri.po_item_number AS EBELP,
        SUM(gri.quantity) AS gr_qty
    FROM gr_item gri
    WHERE gri.movement_type = '101'
        AND gri.quantity > 0
        AND gri.purchase_order_number IS NOT NULL
        AND gri.purchase_order_number != ''
    GROUP BY gri.purchase_order_number, gri.po_item_number
),

invoice_agg AS (
    SELECT
        ii.purchase_order_number AS EBELN,
        ii.po_item_number AS EBELP,
        SUM(ii.invoice_quantity) AS invoice_qty
    FROM inv_item ii
    WHERE ii.invoice_quantity > 0
    GROUP BY ii.purchase_order_number, ii.po_item_number
),

three_way AS (
    SELECT
        po.vendor_id,
        po.month,
        po.EBELN,
        po.EBELP,
        po.po_qty,
        COALESCE(gr.gr_qty, 0) AS gr_qty,
        COALESCE(inv.invoice_qty, 0) AS invoice_qty,
        CASE
            WHEN po.po_qty > 0 AND gr.gr_qty > 0
                AND ABS(po.po_qty - gr.gr_qty) / po.po_qty < 0.05
                AND ABS(gr.gr_qty - inv.invoice_qty) / gr.gr_qty < 0.05
            THEN 1
            ELSE 0
        END AS matched_flag
    FROM po_lines po
    LEFT JOIN gr_agg gr
        ON po.EBELN = gr.EBELN AND po.EBELP = gr.EBELP
    LEFT JOIN invoice_agg inv
        ON po.EBELN = inv.EBELN AND po.EBELP = inv.EBELP
),

exceptions AS (
    SELECT
        za.purchase_order_number AS EBELN,
        za.po_item_number AS EBELP,
        COUNT(*) AS exception_count,
        SUM(CASE WHEN za.approval_status = '02' THEN 1 ELSE 0 END) AS approved_exception_count
    FROM zmm_app za
    WHERE za.purchase_order_number IS NOT NULL
        AND za.po_item_number IS NOT NULL
    GROUP BY za.purchase_order_number, za.po_item_number
)

SELECT
    tw.vendor_id,
    tw.month,
    COUNT(*) AS total_po_lines,
    ROUND(100.0 * SUM(tw.matched_flag) / COUNT(*), 2) AS matched_pct,
    COALESCE(SUM(ex.exception_count), 0) AS exception_count,
    COALESCE(SUM(ex.approved_exception_count), 0) AS approved_exception_count,
    COALESCE(SUM(ex.exception_count - ex.approved_exception_count), 0) AS open_exception_count
FROM three_way tw
LEFT JOIN exceptions ex
    ON tw.EBELN = ex.EBELN AND tw.EBELP = ex.EBELP
GROUP BY tw.vendor_id, tw.month
ORDER BY tw.vendor_id, tw.month
