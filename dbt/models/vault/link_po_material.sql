{{ config(materialized='incremental', unique_key='hk_po_material') }}

/*
    Link: Purchase Order ↔ Material (PO line item level)
    Source: stg_sap__ekpo
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['EBELN', 'EBELP', 'MATNR']) }} AS hk_po_material,
        hk_purchase_order,
        hk_material,
        EBELN AS purchase_order_number,
        EBELP AS po_item_number,
        MATNR AS material_number,
        load_date,
        record_source
    FROM {{ ref('stg_sap__ekpo') }}
)

SELECT
    hk_po_material,
    hk_purchase_order,
    hk_material,
    purchase_order_number,
    po_item_number,
    material_number,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_po_material') }}
