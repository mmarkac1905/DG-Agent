{{ config(materialized='incremental', unique_key=['hk_so_equipment', 'load_date']) }}

/*
    Satellite: Sales Order Item (service line + device)
    Parent: link_sales_order_equipment  (PK hk_so_equipment = VBELN+POSNR+SERNR)
    Item-grain sat hangs off the unit-of-work link per anti-pattern #17.
    Source: stg_sap__vbap
*/

WITH src AS (
    SELECT
        {{ hash_key(['VBELN', 'POSNR', 'SERNR']) }} AS hk_so_equipment,
        hashdiff_vbap AS hashdiff,
        MATNR AS service_plan_id,
        ARKTX AS service_plan_name,
        MATKL AS service_plan_group,
        KWMENG AS quantity,
        VRKME AS unit,
        NETWR AS item_value,
        SERNR AS device_serial,
        WERKS AS plant,
        load_date,
        record_source
    FROM {{ ref('stg_sap__vbap') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_so_equipment = src.hk_so_equipment
      AND t.hashdiff = src.hashdiff
)
{% endif %}
