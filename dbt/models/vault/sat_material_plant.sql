{{ config(materialized='incremental', unique_key=['hk_material_plant', 'load_date']) }}

/*
    Satellite: Material Plant-Level Data (MRP settings, serial profile, lead time)
    Parent: composite hk_material_plant
    Source: stg_sap__marc
*/

WITH src AS (
    SELECT
        hk_material_plant,
        hk_material,
        hk_plant,
        hashdiff_marc AS hashdiff,
        DISMM AS mrp_type,
        DISPO AS mrp_controller,
        EKGRP AS purchasing_group,
        BESKZ AS procurement_type,
        LGPRO AS production_storage_location,
        PLIFZ AS planned_delivery_time_days,
        SERNP AS serial_number_profile,
        load_date,
        record_source
    FROM {{ ref('stg_sap__marc') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_material_plant = src.hk_material_plant
      AND t.hashdiff = src.hashdiff
)
{% endif %}
