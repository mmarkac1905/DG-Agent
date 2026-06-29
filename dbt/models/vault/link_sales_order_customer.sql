{{ config(materialized='incremental', unique_key='hk_sales_order_customer') }}

/*
    Link: Sales Order ↔ Customer (who holds the contract)
    Source: stg_sap__vbak
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['VBELN', 'KUNNR']) }} AS hk_sales_order_customer,
        hk_sales_order,
        hk_customer,
        VBELN AS sales_order_number,
        KUNNR AS customer_id,
        load_date,
        record_source
    FROM {{ ref('stg_sap__vbak') }}
)

SELECT * FROM src
{{ vault_source_filter('src', 'hk_sales_order_customer') }}
