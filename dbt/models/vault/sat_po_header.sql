{{ config(materialized='incremental', unique_key=['hk_purchase_order', 'load_date']) }}

/*
    Satellite: PO Header
    Parent: hub_purchase_order
    Source: stg_sap__ekko
*/

WITH src AS (
    SELECT
        hk_purchase_order,
        hashdiff_ekko AS hashdiff,
        BSTYP AS document_category,
        BSART AS document_type,
        EKORG AS purchasing_organization,
        EKGRP AS purchasing_group,
        BEDAT AS po_date,
        WAERS AS currency,
        RLWRT AS total_po_value,
        BANFN AS requisition_reference,
        ERNAM AS created_by,
        AEDAT AS last_changed_date,
        PROCSTAT AS processing_status,
        load_date,
        record_source
    FROM {{ ref('stg_sap__ekko') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_purchase_order = src.hk_purchase_order
      AND t.hashdiff = src.hashdiff
)
{% endif %}
