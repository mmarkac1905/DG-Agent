{{ config(materialized='incremental', unique_key='hk_movement_type') }}

/*
    Hub: Movement Type
    Business key: BWART (3-char SAP movement type code, e.g. '101', '201')
    Source: stg_sap__t156
*/

SELECT DISTINCT
    hk_movement_type,
    BWART AS movement_type,
    load_date,
    record_source
FROM {{ ref('stg_sap__t156') }} src
{{ vault_source_filter('src', 'hk_movement_type') }}
