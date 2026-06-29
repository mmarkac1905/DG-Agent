{{ config(materialized='incremental', unique_key=['hk_equipment', 'load_date']) }}

/*
    Satellite: Equipment General (static device info)
    Parent: hub_equipment
    Source: stg_sap__equi
*/

WITH src AS (
    SELECT
        hk_equipment,
        hashdiff_equi AS hashdiff,
        SERGE AS serial_number,
        HERST AS manufacturer,
        TYPBZ AS model_description,
        INBDT AS startup_date,
        ERDAT AS created_date,
        ERNAM AS created_by,
        GEWRK AS plant,
        EQART AS equipment_category,
        load_date,
        record_source
    FROM {{ ref('stg_sap__equi') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_equipment = src.hk_equipment
      AND t.hashdiff = src.hashdiff
)
{% endif %}
