{{ config(materialized='incremental', unique_key=['hk_vendor', 'load_date']) }}

/*
    Satellite: Vendor General Data
    Parent: hub_vendor
    Source: stg_sap__lfa1
*/

WITH src AS (
    SELECT
        hk_vendor,
        hashdiff_lfa1 AS hashdiff,
        NAME1 AS vendor_name,
        LAND1 AS country_code,
        ORT01 AS city,
        STRAS AS street_address,
        TELF1 AS phone_number,
        ADRNR AS address_number,
        ERDAT AS created_date,
        ERNAM AS created_by,
        load_date,
        record_source
    FROM {{ ref('stg_sap__lfa1') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_vendor = src.hk_vendor
      AND t.hashdiff = src.hashdiff
)
{% endif %}
