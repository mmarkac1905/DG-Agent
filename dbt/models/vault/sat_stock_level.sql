{{ config(materialized='incremental', unique_key=['hk_stock_location', 'load_date']) }}

/*
    Satellite: Stock Levels
    Parent: composite hk_stock_location (material + plant + storage location)
    Source: stg_sap__mard
*/

WITH src AS (
    SELECT
        hk_stock_location,
        hk_material,
        hk_plant,
        hashdiff_mard AS hashdiff,
        LGORT AS storage_location,
        LABST AS unrestricted_stock,
        INSME AS quality_inspection_stock,
        SPEME AS blocked_stock,
        load_date,
        record_source
    FROM {{ ref('stg_sap__mard') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_stock_location = src.hk_stock_location
      AND t.hashdiff = src.hashdiff
)
{% endif %}
