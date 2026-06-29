{{ config(materialized='incremental', unique_key='hk_invoice') }}

/*
    Hub: Invoice
    Business key: BELNR + GJAHR (composite)
    Source: stg_sap__rbkp
*/

SELECT DISTINCT
    hk_invoice,
    BELNR AS invoice_number,
    GJAHR AS fiscal_year,
    load_date,
    record_source
FROM {{ ref('stg_sap__rbkp') }} src
{{ vault_source_filter('src', 'hk_invoice') }}
