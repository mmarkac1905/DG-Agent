{{ config(materialized='incremental', unique_key='hk_purchase_order') }}

/*
    Hub: Purchase Order
    Business key: EBELN
    Source: stg_sap__ekko
*/

SELECT DISTINCT
    hk_purchase_order,
    EBELN AS purchase_order_number,
    load_date,
    record_source
FROM {{ ref('stg_sap__ekko') }} src
{{ vault_source_filter('src', 'hk_purchase_order') }}
