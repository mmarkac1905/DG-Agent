{{ config(materialized='incremental', unique_key='hk_material_document') }}

/*
    Hub: Material Document
    Business key: MBLNR + MJAHR (composite)
    Source: stg_sap__mkpf
*/

SELECT DISTINCT
    hk_material_document,
    MBLNR AS material_document_number,
    MJAHR AS fiscal_year,
    load_date,
    record_source
FROM {{ ref('stg_sap__mkpf') }} src
{{ vault_source_filter('src', 'hk_material_document') }}
