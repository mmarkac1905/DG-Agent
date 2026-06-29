{{ config(materialized='incremental', unique_key='hk_equipment_gr') }}

/*
    Link: Equipment ↔ Material Document (GR)
    Source: stg_sap__seri joined to stg_sap__mkpf to recover MJAHR
    (SERI.MBLNR is the material doc number; MJAHR lives on MKPF).
    Closes the traceability chain: device → GR → PO.
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['s.EQUNR', 's.MBLNR', 'm.MJAHR']) }} AS hk_equipment_gr,
        s.hk_equipment,
        m.hk_material_document,
        s.EQUNR AS equipment_number,
        s.MBLNR AS material_document_number,
        m.MJAHR AS fiscal_year,
        s.load_date,
        s.record_source
    FROM {{ ref('stg_sap__seri') }} s
    JOIN {{ ref('stg_sap__mkpf') }} m ON m.MBLNR = s.MBLNR
    WHERE s.EQUNR IS NOT NULL AND s.EQUNR != ''
)

SELECT
    hk_equipment_gr,
    hk_equipment,
    hk_material_document,
    equipment_number,
    material_document_number,
    fiscal_year,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_equipment_gr') }}
