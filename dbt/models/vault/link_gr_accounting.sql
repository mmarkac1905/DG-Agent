{{ config(materialized='incremental', unique_key='hk_gr_accounting') }}

/*
    FI Link 3/3: Goods Receipt ↔ Accounting Document (inventory side, BLART='WE')
    Resolves bkpf.AWKEY (= MKPF.MBLNR ‖ MJAHR) for WE postings. AWKEY was
    backfilled from the deterministic WE↔material-document generation order
    (verified 1:1 on BUDAT + BKTXT, 31,963/31,963).
    Source: stg_sap__bkpf (WE) JOIN stg_sap__mkpf
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['m.MBLNR', 'm.MJAHR', 'b.BELNR', 'b.BUKRS']) }} AS hk_gr_accounting,
        m.hk_material_document,
        b.hk_accounting_document,
        m.MBLNR AS material_document_number,
        b.BELNR AS accounting_document_number,
        b.GJAHR AS fiscal_year,
        b.load_date,
        b.record_source
    FROM {{ ref('stg_sap__bkpf') }} b
    JOIN {{ ref('stg_sap__mkpf') }} m ON b.AWKEY = m.MBLNR || m.MJAHR
    WHERE b.BLART = 'WE'
)

SELECT * FROM src
{{ vault_source_filter('src', 'hk_gr_accounting') }}
