{{ config(materialized='incremental', unique_key=['hk_customer', 'load_date']) }}

/*
    Satellite: Customer
    Parent: hub_customer
    Source: stg_sap__kna1
*/

WITH src AS (
    SELECT
        hk_customer,
        hashdiff_kna1 AS hashdiff,
        NAME1 AS customer_name,
        LAND1 AS country,
        ORT01 AS city,
        STRAS AS street,
        PSTLZ AS postal_code,
        KTOKD AS account_group,
        ERDAT AS created_date,
        load_date,
        record_source
    FROM {{ ref('stg_sap__kna1') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_customer = src.hk_customer
      AND t.hashdiff = src.hashdiff
)
{% endif %}
