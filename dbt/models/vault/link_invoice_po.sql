{{ config(materialized='incremental', unique_key='hk_invoice_po') }}

/*
    Link: Invoice ↔ Purchase Order
    Source: stg_sap__rbkp (only where EBELN is populated)
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['BELNR', 'GJAHR', 'EBELN']) }} AS hk_invoice_po,
        hk_invoice,
        hk_purchase_order,
        BELNR AS invoice_number,
        GJAHR AS fiscal_year,
        EBELN AS purchase_order_number,
        load_date,
        record_source
    FROM {{ ref('stg_sap__rbkp') }}
    WHERE EBELN IS NOT NULL AND EBELN != ''
)

SELECT
    hk_invoice_po,
    hk_invoice,
    hk_purchase_order,
    invoice_number,
    fiscal_year,
    purchase_order_number,
    load_date,
    record_source
FROM src
{{ vault_source_filter('src', 'hk_invoice_po') }}
