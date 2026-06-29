{{ config(materialized='incremental', unique_key='hk_pr_po') }}

/*
    Link: Purchase Requisition ↔ Purchase Order
    Source: stg_sap__ekpo (BANFN populated)
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['BANFN', 'EBELN']) }} AS hk_pr_po,
        {{ hash_key(['BANFN']) }} AS hk_purchase_requisition,
        hk_purchase_order,
        BANFN AS requisition_number,
        EBELN AS purchase_order_number,
        load_date,
        record_source
    FROM {{ ref('stg_sap__ekpo') }}
    WHERE BANFN IS NOT NULL AND BANFN != ''
)

SELECT
    hk_pr_po,
    hk_purchase_requisition,
    hk_purchase_order,
    requisition_number,
    purchase_order_number,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_pr_po') }}
