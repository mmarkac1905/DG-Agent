-- Every PO should have a vendor
SELECT purchase_order_number
FROM {{ ref('fact_purchase_orders') }}
WHERE vendor_id IS NULL
