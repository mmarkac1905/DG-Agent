-- Lead time should never be negative (would mean GR before PO)
SELECT purchase_order_number, lead_time_days
FROM {{ ref('fact_purchase_orders') }}
WHERE lead_time_days IS NOT NULL AND lead_time_days < 0
