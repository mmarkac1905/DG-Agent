{{ config(materialized='view') }}

/*
    OBT: Vendor Scorecard — quarterly vendor performance.
    Implements BG001 (lead time), BG002 (OTD), BG010 (concentration risk).
*/

WITH vendor_quarterly AS (
    SELECT
        v.vendor_id,
        v.vendor_name,
        v.vendor_region,
        v.payment_terms,
        d.year,
        d.quarter,
        d.year_quarter,
        COUNT(*) AS total_po_items,
        SUM(f.net_value) AS total_spend,
        AVG(f.lead_time_days) AS avg_lead_time_days,
        AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) AS on_time_delivery_rate,
        SUM(f.ordered_quantity) AS total_ordered_qty,
        SUM(f.received_quantity) AS total_received_qty
    FROM {{ ref('fact_purchase_orders') }} f
    JOIN {{ ref('dim_vendor') }} v ON f.hk_vendor = v.hk_vendor
    JOIN {{ ref('dim_date') }} d ON f.po_date = d.date_key
    WHERE f.po_date IS NOT NULL
    GROUP BY v.vendor_id, v.vendor_name, v.vendor_region, v.payment_terms, d.year, d.quarter, d.year_quarter
),

quarterly_totals AS (
    SELECT year_quarter, SUM(total_spend) AS quarter_total_spend
    FROM vendor_quarterly
    GROUP BY year_quarter
)

SELECT
    vq.*,
    CASE WHEN qt.quarter_total_spend > 0
        THEN ROUND(vq.total_spend / qt.quarter_total_spend, 4)
        ELSE 0
    END AS vendor_spend_share,
    CASE WHEN qt.quarter_total_spend > 0
        AND (vq.total_spend / qt.quarter_total_spend) > 0.60
        THEN TRUE ELSE FALSE
    END AS concentration_risk_flag,
    CASE WHEN vq.total_ordered_qty > 0
        THEN ROUND(vq.total_received_qty / vq.total_ordered_qty, 4)
        ELSE 0
    END AS fulfillment_rate
FROM vendor_quarterly vq
LEFT JOIN quarterly_totals qt ON vq.year_quarter = qt.year_quarter
