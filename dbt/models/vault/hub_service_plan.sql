{{ config(materialized='incremental', unique_key='hk_service_plan') }}

/*
    Hub: Service Plan (broadband / IPTV / business subscription)
    Business key: MATNR (service-plan material)
    Source: stg_sap__vbap
*/

SELECT DISTINCT
    hk_service_plan,
    MATNR AS service_plan_id,
    load_date,
    record_source
FROM {{ ref('stg_sap__vbap') }} src
{{ vault_source_filter('src', 'hk_service_plan') }}
