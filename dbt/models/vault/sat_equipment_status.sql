{{ config(materialized='incremental', unique_key=['hk_equipment', 'status_from_date']) }}

/*
    Satellite: Equipment Status History (SCD2 — lifecycle)
    Parent: hub_equipment
    Source: stg_sap__eqbs
*/

WITH src AS (
    SELECT
        hk_equipment,
        hashdiff_eqbs AS hashdiff,
        BEGDT AS status_from_date,
        USTXT AS status_code,
        STAT_DESC AS status_description,
        load_date,
        record_source
    FROM {{ ref('stg_sap__eqbs') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_equipment = src.hk_equipment
      AND t.status_from_date = src.status_from_date
      AND t.hashdiff = src.hashdiff
)
{% endif %}
