{{ config(materialized='view') }}

SELECT
    f.order_month,
    d.year,
    d.month,
    d.month_name,
    d.year_month,
    f.delivered_orders,
    f.cross_state_orders,
    f.same_state_orders,
    f.cross_state_order_share_pct
FROM {{ ref('fact_cross_state_order_share') }} f
LEFT JOIN {{ ref('dim_date') }}                d
    ON d.date_day = f.order_month
ORDER BY f.order_month
