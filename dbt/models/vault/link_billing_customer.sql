{{ config(materialized='incremental', unique_key='hk_billing_customer') }}

/*
    Link: Billing Document ↔ Customer (who is billed — payer)
    Source: stg_sap__vbrk (KUNRG = payer)
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['VBELN', 'KUNRG']) }} AS hk_billing_customer,
        hk_billing_doc,
        hk_customer,
        VBELN AS billing_document_number,
        KUNRG AS customer_id,
        load_date,
        record_source
    FROM {{ ref('stg_sap__vbrk') }}
)

SELECT * FROM src
{{ vault_source_filter('src', 'hk_billing_customer') }}
