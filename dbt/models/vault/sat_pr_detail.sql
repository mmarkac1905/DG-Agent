{{ config(materialized='incremental', unique_key=['hk_purchase_requisition', 'load_date']) }}

/*
    Satellite: Purchase Requisition Detail
    Parent: hub_purchase_requisition
    Source: stg_sap__eban
*/

WITH src AS (
    SELECT
        hk_purchase_requisition,
        hashdiff_eban AS hashdiff,
        MATNR AS material_number,
        WERKS AS plant,
        LGORT AS storage_location,
        MENGE AS requested_quantity,
        MEINS AS unit_of_measure,
        PREIS AS estimated_price,
        BADAT AS requisition_date,
        FRGDT AS release_date,
        ERNAM AS created_by,
        ESTKZ AS source_indicator,
        STATU AS status,
        EKGRP AS purchasing_group,
        EKORG AS purchasing_organization,
        load_date,
        record_source
    FROM {{ ref('stg_sap__eban') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_purchase_requisition = src.hk_purchase_requisition
      AND t.hashdiff = src.hashdiff
)
{% endif %}
