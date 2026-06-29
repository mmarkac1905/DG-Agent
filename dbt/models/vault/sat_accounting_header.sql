{{ config(materialized='incremental', unique_key=['hk_accounting_document', 'load_date']) }}

/*
    Satellite: Accounting Document Header (FI journal — WE / RE / RV)
    Parent: hub_accounting_document
    Source: stg_sap__bkpf. AWTYP/AWKEY carry the reference back to the source
    logistics document (material doc / MM invoice / SD billing).
*/

WITH src AS (
    SELECT
        hk_accounting_document,
        hashdiff_bkpf AS hashdiff,
        BLART AS document_type,
        BUDAT AS posting_date,
        BLDAT AS document_date,
        USNAM AS posted_by,
        XBLNR AS reference,
        AWTYP AS reference_transaction,
        AWKEY AS reference_key,
        load_date,
        record_source
    FROM {{ ref('stg_sap__bkpf') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_accounting_document = src.hk_accounting_document
      AND t.hashdiff = src.hashdiff
)
{% endif %}
