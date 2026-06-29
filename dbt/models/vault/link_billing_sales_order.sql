{{ config(materialized='incremental', unique_key='hk_billing_sales_order') }}

/*
    Link: Billing Document ↔ Sales Order (bill back to the contract)
    Source: stg_sap__vbrp (VBRP.AUBEL references the originating sales order)
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['VBELN', 'AUBEL']) }} AS hk_billing_sales_order,
        hk_billing_doc,
        hk_sales_order,
        VBELN AS billing_document_number,
        AUBEL AS sales_order_number,
        load_date,
        record_source
    FROM {{ ref('stg_sap__vbrp') }}
    WHERE AUBEL IS NOT NULL AND AUBEL != ''
)

SELECT * FROM src
{{ vault_source_filter('src', 'hk_billing_sales_order') }}
