{{ config(materialized='incremental', unique_key=['hk_gr_material', 'load_date']) }}

/*
    Satellite: Material Document Item
    Parent: link_gr_material
    Source: stg_sap__mseg
*/

WITH src AS (
    SELECT
        {{ hash_key(['MBLNR', 'MJAHR', 'MATNR']) }} AS hk_gr_material,
        hashdiff_mseg AS hashdiff,
        ZEILE AS document_item,
        BWART AS movement_type,
        WERKS AS plant,
        LGORT AS storage_location,
        MENGE AS quantity,
        MEINS AS unit_of_measure,
        EBELN AS purchase_order_number,
        EBELP AS po_item_number,
        DMBTR AS amount_local_currency,
        WAERS AS currency,
        SERNP AS serial_number_profile,
        LIFNR AS vendor_id,
        load_date,
        record_source
    FROM {{ ref('stg_sap__mseg') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_gr_material = src.hk_gr_material
      AND t.hashdiff = src.hashdiff
)
{% endif %}
