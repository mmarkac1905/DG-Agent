-- Every invoice should reference a PO for three-way match
SELECT invoice_number
FROM {{ ref('fact_invoices') }}
WHERE purchase_order_number IS NULL
