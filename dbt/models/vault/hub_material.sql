{{ config(materialized='incremental', unique_key='hk_material') }}

/*
    Hub: Material
    Business key: MATNR
    Source: stg_sap__mara
*/

SELECT DISTINCT
    hk_material,
    MATNR AS material_number,
    load_date,
    record_source
FROM {{ ref('stg_sap__mara') }} src
{{ vault_source_filter('src', 'hk_material') }}
