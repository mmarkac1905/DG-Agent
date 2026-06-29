{{ config(materialized='incremental', unique_key=['hk_material_document', 'load_date']) }}

/*
    Satellite: GR / Material Document Header
    Parent: hub_material_document
    Source: stg_sap__mkpf
*/

WITH src AS (
    SELECT
        hk_material_document,
        hashdiff_mkpf AS hashdiff,
        BUDAT AS posting_date,
        BLDAT AS document_date,
        USNAM AS posted_by,
        BKTXT AS header_text,
        XBLNR AS reference_document,
        load_date,
        record_source
    FROM {{ ref('stg_sap__mkpf') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_material_document = src.hk_material_document
      AND t.hashdiff = src.hashdiff
)
{% endif %}
