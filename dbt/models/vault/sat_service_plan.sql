{{ config(materialized='incremental', unique_key=['hk_service_plan', 'load_date']) }}

/*
    Satellite: Service Plan
    Parent: hub_service_plan
    Source: stg_sap__vbap (distinct plan attributes)
*/

WITH src AS (
    SELECT DISTINCT
        hk_service_plan,
        {{ hashdiff(['ARKTX', 'MATKL']) }} AS hashdiff,
        ARKTX AS service_plan_name,
        MATKL AS service_plan_group,
        load_date,
        record_source
    FROM {{ ref('stg_sap__vbap') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_service_plan = src.hk_service_plan
      AND t.hashdiff = src.hashdiff
)
{% endif %}
