{{ config(materialized='incremental', unique_key='hk_equipment_material') }}

/*
    Link: Equipment ↔ Material
    Source: stg_sap__equi
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['EQUNR', 'MATNR']) }} AS hk_equipment_material,
        hk_equipment,
        hk_material,
        EQUNR AS equipment_number,
        MATNR AS material_number,
        load_date,
        record_source
    FROM {{ ref('stg_sap__equi') }}
)

SELECT
    hk_equipment_material,
    hk_equipment,
    hk_material,
    equipment_number,
    material_number,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_equipment_material') }}
