{{ config(materialized='table') }}

/*
    Fact: Goods Receipt Accuracy — grain = vendor × quarter.
    Counts receipts where GR quantity matches ordered PO quantity, then
    aggregates accuracy by vendor and posting quarter.

    RULE 3 alignment (refactored 2026-05-05): vault-sourced. Joins
    link_gr_material with the latest snapshots of sat_gr_item and
    sat_gr_header for the GR side, and link_po_material + latest
    sat_po_item for the PO side. Previously read stg_sap__mseg +
    stg_sap__mkpf + stg_sap__ekpo directly, violating "marts only ref()
    vault models."
*/

WITH gr_item AS (
    SELECT *
    FROM {{ ref('sat_gr_item') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_gr_item') }})
),

gr_header AS (
    SELECT *
    FROM {{ ref('sat_gr_header') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_gr_header') }})
),

po_item AS (
    SELECT *
    FROM {{ ref('sat_po_item') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_po_item') }})
),

goods_receipts AS (
    SELECT
        gri.purchase_order_number,
        gri.po_item_number,
        gri.vendor_id,
        gri.quantity AS gr_quantity,
        pi.ordered_quantity AS po_quantity,
        DATE_TRUNC('quarter', grh.posting_date) AS quarter,
        grh.posting_date,
        CASE WHEN gri.quantity = pi.ordered_quantity THEN 1 ELSE 0 END AS is_accurate,
        gri.movement_type
    FROM {{ ref('link_gr_material') }} lgm
    JOIN gr_item gri
        ON lgm.hk_gr_material = gri.hk_gr_material
    JOIN gr_header grh
        ON lgm.hk_material_document = grh.hk_material_document
    JOIN {{ ref('link_po_material') }} lpm
        ON gri.purchase_order_number = lpm.purchase_order_number
        AND gri.po_item_number = lpm.po_item_number
        AND lgm.material_number = lpm.material_number
    JOIN po_item pi
        ON lpm.hk_po_material = pi.hk_po_material
    WHERE gri.movement_type = '101'
        AND gri.purchase_order_number IS NOT NULL
        AND gri.vendor_id IS NOT NULL
        AND pi.ordered_quantity IS NOT NULL
        AND gri.quantity IS NOT NULL
)

SELECT
    vendor_id,
    quarter,
    COUNT(*) AS total_receipts,
    SUM(is_accurate) AS accurate_receipts,
    ROUND(AVG(is_accurate) * 100, 2) AS accuracy_percentage,
    MIN(posting_date) AS first_receipt_date,
    MAX(posting_date) AS last_receipt_date
FROM goods_receipts
GROUP BY vendor_id, quarter
ORDER BY quarter DESC, accuracy_percentage DESC
