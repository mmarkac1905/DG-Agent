{{ config(materialized='incremental', unique_key=['hk_sales_order', 'load_date']) }}

/*
    Satellite: Sales Order (contract header)
    Parent: hub_sales_order
    Source: stg_sap__vbak
*/

WITH src AS (
    SELECT
        hk_sales_order,
        hashdiff_vbak AS hashdiff,
        AUDAT AS order_date,
        AUART AS order_type,
        VKORG AS sales_org,
        VTWEG AS distribution_channel,
        SPART AS division,
        KUNNR AS customer_id,
        NETWR AS order_value,
        WAERK AS currency,
        load_date,
        record_source
    FROM {{ ref('stg_sap__vbak') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_sales_order = src.hk_sales_order
      AND t.hashdiff = src.hashdiff
)
{% endif %}
