{{ config(materialized='incremental', unique_key=['hk_invoice', 'load_date']) }}

/*
    Satellite: Invoice Header
    Parent: hub_invoice
    Source: stg_sap__rbkp
*/

WITH src AS (
    SELECT
        hk_invoice,
        hashdiff_rbkp AS hashdiff,
        BLDAT AS invoice_date,
        BUDAT AS posting_date,
        LIFNR AS vendor_id,
        WAERS AS currency,
        RMWWR AS invoice_total_amount,
        XBLNR AS vendor_invoice_reference,
        EBELN AS purchase_order_reference,
        USNAM AS posted_by,
        load_date,
        record_source
    FROM {{ ref('stg_sap__rbkp') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_invoice = src.hk_invoice
      AND t.hashdiff = src.hashdiff
)
{% endif %}
