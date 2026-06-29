{{ config(materialized='incremental', unique_key='hk_accounting_document') }}

/*
    Hub: Accounting Document (FI journal entry — WE / RE / RV)
    Business key: BELNR + GJAHR + BUKRS (composite)
    Source: stg_sap__bkpf
*/

SELECT DISTINCT
    hk_accounting_document,
    BELNR AS accounting_document_number,
    GJAHR AS fiscal_year,
    BUKRS AS company_code,
    load_date,
    record_source
FROM {{ ref('stg_sap__bkpf') }} src
{{ vault_source_filter('src', 'hk_accounting_document') }}
