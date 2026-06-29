{{ config(materialized='incremental', unique_key='hk_equipment') }}

/*
    Hub: Equipment (individual CPE device)
    Business key: EQUNR
    Source: stg_sap__equi
*/

SELECT DISTINCT
    hk_equipment,
    EQUNR AS equipment_number,
    load_date,
    record_source
FROM {{ ref('stg_sap__equi') }} src
{{ vault_source_filter('src', 'hk_equipment') }}
