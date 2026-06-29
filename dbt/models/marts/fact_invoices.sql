{{ config(materialized='table') }}

/*
    Fact: Invoices — grain = invoice.
*/

SELECT
    hi.hk_invoice,
    {{ hash_key(['ih.vendor_id']) }} AS hk_vendor,
    lip.hk_purchase_order,
    hi.invoice_number,
    hi.fiscal_year,
    ih.vendor_id,
    lip.purchase_order_number,
    ih.invoice_date,
    ih.posting_date,
    ih.currency,
    ih.invoice_total_amount,
    ih.vendor_invoice_reference,
    ih.posted_by,
    CASE WHEN ih.invoice_date IS NOT NULL AND ph.po_date IS NOT NULL
        THEN CAST(ih.invoice_date - ph.po_date AS INTEGER)
        ELSE NULL
    END AS days_po_to_invoice,
    ih.record_source

FROM {{ ref('hub_invoice') }} hi
LEFT JOIN {{ ref('sat_invoice_header') }} ih
    ON hi.hk_invoice = ih.hk_invoice
    AND ih.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_invoice_header') }} WHERE hk_invoice = hi.hk_invoice)
LEFT JOIN {{ ref('link_invoice_po') }} lip ON hi.hk_invoice = lip.hk_invoice
LEFT JOIN {{ ref('sat_po_header') }} ph ON lip.hk_purchase_order = ph.hk_purchase_order
    AND ph.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_po_header') }} WHERE hk_purchase_order = lip.hk_purchase_order)
