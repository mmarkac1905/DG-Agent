{{ config(materialized='incremental', unique_key='hk_po_plant') }}

/*
    Link: Purchase Order ↔ Plant
    Source: stg_sap__ekpo (hk_plant derived inline from WERKS)
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['EBELN', 'WERKS']) }} AS hk_po_plant,
        hk_purchase_order,
        {{ hash_key(['WERKS']) }} AS hk_plant,
        EBELN AS purchase_order_number,
        WERKS AS plant_code,
        load_date,
        record_source
    FROM {{ ref('stg_sap__ekpo') }}
)

SELECT
    hk_po_plant,
    hk_purchase_order,
    hk_plant,
    purchase_order_number,
    plant_code,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_po_plant') }}
