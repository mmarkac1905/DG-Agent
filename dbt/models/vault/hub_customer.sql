{{ config(materialized='incremental', unique_key='hk_customer') }}

/*
    Hub: Customer
    Business key: KUNNR
    Source: stg_sap__kna1
*/

SELECT DISTINCT
    hk_customer,
    KUNNR AS customer_id,
    load_date,
    record_source
FROM {{ ref('stg_sap__kna1') }} src
{{ vault_source_filter('src', 'hk_customer') }}
