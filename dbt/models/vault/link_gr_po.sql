{{ config(materialized='incremental', unique_key='hk_gr_po') }}

/*
    Link: Goods Receipt (Material Document) ↔ Purchase Order
    Source: stg_sap__mseg (only rows with EBELN populated)
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['MBLNR', 'MJAHR', 'EBELN']) }} AS hk_gr_po,
        hk_material_document,
        hk_purchase_order,
        MBLNR AS material_document_number,
        MJAHR AS fiscal_year,
        EBELN AS purchase_order_number,
        load_date,
        record_source
    FROM {{ ref('stg_sap__mseg') }}
    WHERE EBELN IS NOT NULL AND EBELN != ''
)

SELECT
    hk_gr_po,
    hk_material_document,
    hk_purchase_order,
    material_document_number,
    fiscal_year,
    purchase_order_number,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_gr_po') }}
