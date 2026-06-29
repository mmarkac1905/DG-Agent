{{ config(materialized='incremental', unique_key=['hk_material', 'load_date']) }}

/*
    Satellite: Material General Data
    Parent: hub_material
    Source: stg_sap__mara
*/

WITH src AS (
    SELECT
        hk_material,
        hashdiff_mara AS hashdiff,
        MTART AS material_type,
        MATKL AS material_group,
        MEINS AS base_unit_of_measure,
        BRGEW AS gross_weight_kg,
        GEWEI AS weight_unit,
        MSTAE AS cross_plant_status,
        SPART AS division,
        PRDHA AS product_hierarchy,
        ERDAT AS created_date,
        load_date,
        record_source
    FROM {{ ref('stg_sap__mara') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_material = src.hk_material
      AND t.hashdiff = src.hashdiff
)
{% endif %}
