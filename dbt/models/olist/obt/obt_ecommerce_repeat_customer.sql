{{ config(materialized='view') }}

SELECT
    f.order_month,
    d.year,
    d.month,
    d.month_name,
    d.year_month,
    d.quarter,
    d.year_quarter,
    f.total_orders,
    f.repeat_orders,
    f.total_orders - f.repeat_orders               AS first_time_orders,
    f.repeat_customer_rate_pct
FROM {{ ref('fact_repeat_customer_rate') }}  f
LEFT JOIN {{ ref('dim_date') }}              d  ON d.date_day = f.order_month
ORDER BY f.order_month
