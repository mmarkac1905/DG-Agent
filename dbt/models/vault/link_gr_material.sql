{{ config(materialized='incremental', unique_key='hk_gr_material') }}

/*
    Link: Goods Receipt ↔ Material
    Source: stg_sap__mseg
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['MBLNR', 'MJAHR', 'MATNR']) }} AS hk_gr_material,
        hk_material_document,
        hk_material,
        MBLNR AS material_document_number,
        MJAHR AS fiscal_year,
        MATNR AS material_number,
        load_date,
        record_source
    FROM {{ ref('stg_sap__mseg') }}
)

SELECT
    hk_gr_material,
    hk_material_document,
    hk_material,
    material_document_number,
    fiscal_year,
    material_number,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_gr_material') }}
