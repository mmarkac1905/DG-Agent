{{ config(materialized='incremental', unique_key='hk_purchase_requisition') }}

/*
    Hub: Purchase Requisition
    Business key: BANFN
    Source: stg_sap__eban
*/

SELECT DISTINCT
    hk_purchase_requisition,
    BANFN AS requisition_number,
    load_date,
    record_source
FROM {{ ref('stg_sap__eban') }} src
{{ vault_source_filter('src', 'hk_purchase_requisition') }}
