{{ config(materialized='incremental', unique_key='hk_plant') }}

/*
    Hub: Plant
    Business key: WERKS
    Source: stg_sap__t001w
*/

SELECT DISTINCT
    hk_plant,
    WERKS AS plant_code,
    load_date,
    record_source
FROM {{ ref('stg_sap__t001w') }} src
{{ vault_source_filter('src', 'hk_plant') }}
