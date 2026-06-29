{{ config(materialized='incremental', unique_key='hk_vendor') }}

/*
    Hub: Vendor
    Business key: LIFNR
    Source: stg_sap__lfa1
*/

SELECT DISTINCT
    hk_vendor,
    LIFNR AS vendor_id,
    load_date,
    record_source
FROM {{ ref('stg_sap__lfa1') }} src
{{ vault_source_filter('src', 'hk_vendor') }}
