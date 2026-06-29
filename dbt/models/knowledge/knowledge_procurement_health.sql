{{ config(materialized='view') }}

/*
    Knowledge: Procurement Health Dashboard
    Live KPIs with GREEN/YELLOW/RED health assessments.
*/

WITH po_metrics AS (
    SELECT
        COUNT(*) AS total_po_items,
        COUNT(DISTINCT purchase_order_number) AS total_pos,
        ROUND(AVG(lead_time_days), 1) AS avg_lead_time_days,
        ROUND(AVG(CASE WHEN is_on_time THEN 1.0 ELSE 0.0 END) * 100, 1) AS otd_rate_pct,
        ROUND(AVG(po_cycle_days), 1) AS avg_po_cycle_days,
        SUM(net_value) AS total_spend_eur,
        COUNT(CASE WHEN delivery_status = 'pending' THEN 1 END) AS pending_deliveries,
        COUNT(CASE WHEN delivery_status = 'partially_received' THEN 1 END) AS partial_deliveries
    FROM {{ ref('fact_purchase_orders') }}
),

vendor_concentration AS (
    SELECT
        vendor_name,
        ROUND(MAX(vendor_spend_share) * 100, 1) AS max_spend_share_pct,
        BOOL_OR(concentration_risk_flag) AS has_concentration_risk
    FROM {{ ref('obt_vendor_scorecard') }}
    GROUP BY vendor_name
    ORDER BY max_spend_share_pct DESC
    LIMIT 1
),

inventory_alerts AS (
    SELECT
        COUNT(CASE WHEN is_zero_stock AND total_stock = 0 THEN 1 END) AS zero_stock_locations,
        COUNT(CASE WHEN has_blocked_stock THEN 1 END) AS blocked_stock_locations,
        ROUND(SUM(total_stock), 0) AS total_stock_units
    FROM {{ ref('fact_inventory') }}
)

SELECT
    pm.total_pos,
    pm.total_po_items,
    pm.avg_lead_time_days,
    pm.otd_rate_pct,
    pm.avg_po_cycle_days,
    pm.total_spend_eur,
    pm.pending_deliveries,
    pm.partial_deliveries,

    CASE
        WHEN pm.otd_rate_pct >= 80 THEN 'GREEN'
        WHEN pm.otd_rate_pct >= 60 THEN 'YELLOW'
        ELSE 'RED'
    END AS otd_health,

    CASE
        WHEN pm.avg_lead_time_days <= 35 THEN 'GREEN'
        WHEN pm.avg_lead_time_days <= 50 THEN 'YELLOW'
        ELSE 'RED'
    END AS lead_time_health,

    CASE
        WHEN pm.avg_po_cycle_days <= 5 THEN 'GREEN'
        WHEN pm.avg_po_cycle_days <= 10 THEN 'YELLOW'
        ELSE 'RED'
    END AS cycle_time_health,

    vc.vendor_name AS highest_concentration_vendor,
    vc.max_spend_share_pct AS highest_concentration_pct,
    vc.has_concentration_risk,
    CASE
        WHEN vc.has_concentration_risk THEN 'RED'
        WHEN vc.max_spend_share_pct > 50 THEN 'YELLOW'
        ELSE 'GREEN'
    END AS concentration_health,

    ia.total_stock_units,
    ia.zero_stock_locations,
    ia.blocked_stock_locations,
    CASE
        WHEN ia.zero_stock_locations > 5 THEN 'RED'
        WHEN ia.zero_stock_locations > 0 THEN 'YELLOW'
        ELSE 'GREEN'
    END AS inventory_health

FROM po_metrics pm
CROSS JOIN vendor_concentration vc
CROSS JOIN inventory_alerts ia
