{{ config(materialized='incremental', unique_key=['hk_po_item', 'load_date']) }}

/*
    Satellite: PO Account Assignment
    Parent: PO item
    Source: stg_sap__ekkn
*/

WITH src AS (
    SELECT
        hk_po_item,
        hashdiff_ekkn AS hashdiff,
        ZEKKN AS account_assignment_number,
        SAKTO AS gl_account,
        KOSTL AS cost_center,
        load_date,
        record_source
    FROM {{ ref('stg_sap__ekkn') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_po_item = src.hk_po_item
      AND t.hashdiff = src.hashdiff
)
{% endif %}
