{{ config(materialized='view') }}

/*
    Knowledge: Vendor Performance Summary — one row per vendor with grade.
*/

SELECT
    v.vendor_id,
    v.vendor_name,
    v.country_code,
    v.vendor_region,
    v.payment_terms,
    COUNT(DISTINCT f.purchase_order_number) AS total_pos,
    SUM(f.net_value) AS total_spend_eur,
    ROUND(AVG(f.lead_time_days), 1) AS avg_lead_time_days,
    ROUND(AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) * 100, 1) AS otd_rate_pct,
    SUM(f.ordered_quantity) AS total_ordered_qty,
    SUM(f.received_quantity) AS total_received_qty,
    ROUND(SUM(f.received_quantity) / NULLIF(SUM(f.ordered_quantity), 0) * 100, 1) AS fulfillment_rate_pct,
    CASE
        WHEN AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) >= 0.80 THEN 'A'
        WHEN AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) >= 0.60 THEN 'B'
        WHEN AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) >= 0.40 THEN 'C'
        ELSE 'D'
    END AS performance_grade,
    CASE
        WHEN AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) >= 0.80 THEN 'GREEN'
        WHEN AVG(CASE WHEN f.is_on_time THEN 1.0 ELSE 0.0 END) >= 0.60 THEN 'YELLOW'
        ELSE 'RED'
    END AS health_status
FROM {{ ref('dim_vendor') }} v
LEFT JOIN {{ ref('fact_purchase_orders') }} f ON v.hk_vendor = f.hk_vendor
GROUP BY v.vendor_id, v.vendor_name, v.country_code, v.vendor_region, v.payment_terms
