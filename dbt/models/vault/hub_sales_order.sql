{{ config(materialized='incremental', unique_key='hk_sales_order') }}

/*
    Hub: Sales Order (service contract)
    Business key: VBELN
    Source: stg_sap__vbak
*/

SELECT DISTINCT
    hk_sales_order,
    VBELN AS sales_order_number,
    load_date,
    record_source
FROM {{ ref('stg_sap__vbak') }} src
{{ vault_source_filter('src', 'hk_sales_order') }}
