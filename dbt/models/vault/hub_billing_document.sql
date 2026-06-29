{{ config(materialized='incremental', unique_key='hk_billing_doc') }}

/*
    Hub: Billing Document (monthly service invoice)
    Business key: VBELN (billing)
    Source: stg_sap__vbrk
*/

SELECT DISTINCT
    hk_billing_doc,
    VBELN AS billing_document_number,
    load_date,
    record_source
FROM {{ ref('stg_sap__vbrk') }} src
{{ vault_source_filter('src', 'hk_billing_doc') }}
