{{ config(materialized='incremental', unique_key='hk_invoice_accounting') }}

/*
    FI Link 1/3: MM Invoice ↔ Accounting Document (cost side, BLART='RE')
    Resolves bkpf.AWKEY (= RBKP.BELNR ‖ GJAHR) for RE postings.
    Source: stg_sap__bkpf (RE) JOIN stg_sap__rbkp
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['r.BELNR', 'r.GJAHR', 'b.BELNR', 'b.BUKRS']) }} AS hk_invoice_accounting,
        r.hk_invoice,
        b.hk_accounting_document,
        r.BELNR AS invoice_number,
        b.BELNR AS accounting_document_number,
        b.GJAHR AS fiscal_year,
        b.load_date,
        b.record_source
    FROM {{ ref('stg_sap__bkpf') }} b
    JOIN {{ ref('stg_sap__rbkp') }} r ON b.AWKEY = r.BELNR || r.GJAHR
    WHERE b.BLART = 'RE'
)

SELECT * FROM src
{{ vault_source_filter('src', 'hk_invoice_accounting') }}
