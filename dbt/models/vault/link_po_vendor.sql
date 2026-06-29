{{ config(materialized='incremental', unique_key='hk_po_vendor') }}

/*
    Link: Purchase Order ↔ Vendor
    Source: stg_sap__ekko
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['EBELN', 'LIFNR']) }} AS hk_po_vendor,
        hk_purchase_order,
        hk_vendor,
        EBELN AS purchase_order_number,
        LIFNR AS vendor_id,
        load_date,
        record_source
    FROM {{ ref('stg_sap__ekko') }}
)

SELECT
    hk_po_vendor,
    hk_purchase_order,
    hk_vendor,
    purchase_order_number,
    vendor_id,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_po_vendor') }}
