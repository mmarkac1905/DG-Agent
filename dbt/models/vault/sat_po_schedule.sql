{{ config(materialized='incremental', unique_key=['hk_po_item', 'load_date']) }}

/*
    Satellite: PO Delivery Schedule
    Parent: PO item (hk_po_item composite)
    Source: stg_sap__eket
*/

WITH src AS (
    SELECT
        hk_po_item,
        hashdiff_eket AS hashdiff,
        ETENR AS schedule_line,
        EINDT AS scheduled_delivery_date,
        MENGE AS scheduled_quantity,
        WEMNG AS goods_received_quantity,
        load_date,
        record_source
    FROM {{ ref('stg_sap__eket') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_po_item = src.hk_po_item
      AND t.hashdiff = src.hashdiff
)
{% endif %}
