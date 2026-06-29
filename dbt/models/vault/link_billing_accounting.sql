{{ config(materialized='incremental', unique_key='hk_billing_accounting') }}

/*
    FI Link 2/3: SD Billing ↔ Accounting Document (revenue side, BLART='RV')
    Resolves bkpf.AWKEY (= VBRK.VBELN ‖ GJAHR) for RV postings.
    Source: stg_sap__bkpf (RV) JOIN stg_sap__vbrk
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['v.VBELN', 'b.BELNR', 'b.GJAHR', 'b.BUKRS']) }} AS hk_billing_accounting,
        v.hk_billing_doc,
        b.hk_accounting_document,
        v.VBELN AS billing_document_number,
        b.BELNR AS accounting_document_number,
        b.GJAHR AS fiscal_year,
        b.load_date,
        b.record_source
    FROM {{ ref('stg_sap__bkpf') }} b
    -- Join key must be single-table on each side or DuckDB falls back to a
    -- nested-loop join (295k x 301k). AWKEY = VBELN || fiscal-year, and the RV
    -- fiscal year equals YEAR(FKDAT), so derive it from vbrk (no GJAHR column there).
    JOIN {{ ref('stg_sap__vbrk') }} v
        ON b.AWKEY = v.VBELN || CAST(YEAR(v.FKDAT) AS VARCHAR)
    WHERE b.BLART = 'RV'
)

SELECT * FROM src
{{ vault_source_filter('src', 'hk_billing_accounting') }}
