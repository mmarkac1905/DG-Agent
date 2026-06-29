{{ config(materialized='incremental', unique_key=['hk_po_material', 'load_date']) }}

/*
    Satellite: PO Item Details
    Parent: link_po_material
    Source: stg_sap__ekpo
*/

WITH src AS (
    SELECT
        {{ hash_key(['EBELN', 'EBELP', 'MATNR']) }} AS hk_po_material,
        hashdiff_ekpo AS hashdiff,
        TXZ01 AS item_short_text,
        MENGE AS ordered_quantity,
        MEINS AS unit_of_measure,
        NETPR AS unit_price,
        NETWR AS net_value,
        WERKS AS plant,
        LGORT AS storage_location,
        MATKL AS material_group,
        PSTYP AS item_category,
        ELIKZ AS delivery_completed_flag,
        load_date,
        record_source
    FROM {{ ref('stg_sap__ekpo') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_po_material = src.hk_po_material
      AND t.hashdiff = src.hashdiff
)
{% endif %}
