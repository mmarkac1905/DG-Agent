{{ config(materialized='incremental', unique_key=['hk_invoice', 'invoice_line_item', 'load_date']) }}

/*
    Satellite: Invoice Item (RSEG line-item details)
    Parent: hub_invoice (multi-row sat — one row per BUZEI line item)
    Source: stg_sap__rseg

    Note: stg_sap__rseg's hk_invoice is hash(BELNR, GJAHR), so multiple
    line items share the same hk_invoice. The compound unique_key
    includes invoice_line_item (BUZEI) so the satellite holds one row
    per (invoice, line_item, load_date). Carries EBELN/EBELP as natural
    keys so marts can join to PO items without a dedicated link.
*/

WITH src AS (
    SELECT
        hk_invoice,
        hashdiff_rseg AS hashdiff,
        BELNR AS invoice_number,
        GJAHR AS fiscal_year,
        BUZEI AS invoice_line_item,
        EBELN AS purchase_order_number,
        EBELP AS po_item_number,
        MENGE AS invoice_quantity,
        WRBTR AS invoice_amount,
        WAERS AS currency,
        load_date,
        record_source
    FROM {{ ref('stg_sap__rseg') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_invoice = src.hk_invoice
      AND t.invoice_line_item = src.invoice_line_item
      AND t.hashdiff = src.hashdiff
)
{% endif %}
