{{ config(materialized='table') }}

/*
    Fact: Goods Receipts by Month — grain = posting month.
    BG029 — counts goods-receipt line items into warehouse stock per month.

    RULE 3 alignment (refactored 2026-05-05): vault-sourced. Joins
    link_gr_material with the latest snapshots of sat_gr_item (for the
    BWART='101' filter + LIFNR/MATNR aggregates) and sat_gr_header
    (for the posting_date / BUDAT). Previously read stg_sap__mseg +
    stg_sap__mkpf directly, violating "marts only ref() vault models."
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
)

SELECT
    DATE_TRUNC('month', grh.posting_date) AS receipt_month,
    COUNT(*) AS receipt_line_count,
    COUNT(DISTINCT lgm.material_document_number) AS distinct_documents,
    COUNT(DISTINCT gri.vendor_id) AS distinct_vendors,
    COUNT(DISTINCT lgm.material_number) AS distinct_materials
FROM {{ ref('link_gr_material') }} lgm
JOIN gr_item gri
    ON lgm.hk_gr_material = gri.hk_gr_material
JOIN gr_header grh
    ON lgm.hk_material_document = grh.hk_material_document
WHERE gri.movement_type = '101'
GROUP BY DATE_TRUNC('month', grh.posting_date)
ORDER BY receipt_month
