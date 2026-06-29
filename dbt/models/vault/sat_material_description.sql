{{ config(materialized='incremental', unique_key=['hk_material', 'load_date']) }}

/*
    Satellite: Material Descriptions (English only for MVP)
    Parent: hub_material
    Source: stg_sap__makt
*/

WITH src AS (
    SELECT
        hk_material,
        hashdiff_makt AS hashdiff,
        SPRAS AS language_key,
        MAKTX AS material_description,
        load_date,
        record_source
    FROM {{ ref('stg_sap__makt') }}
    WHERE SPRAS = 'E'
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_material = src.hk_material
      AND t.hashdiff = src.hashdiff
)
{% endif %}
