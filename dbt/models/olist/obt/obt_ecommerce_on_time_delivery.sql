{{ config(
    materialized='view',
    tags=['ecommerce', 'delivery', 'obt']
) }}

WITH fact AS (
    SELECT
        order_month,
        delivered_orders,
        on_time_orders,
        on_time_delivery_rate_pct
    FROM {{ ref('fact_on_time_delivery_monthly') }}
),

date_dim AS (
    SELECT
        date_day,
        year,
        month,
        month_name,
        year_month,
        quarter,
        year_quarter
    FROM {{ ref('dim_date') }}
    WHERE date_day = DATE_TRUNC('month', date_day)  -- one row per month
)

SELECT
    f.order_month,
    d.year,
    d.month,
    d.month_name,
    d.year_month,
    d.quarter,
    d.year_quarter,
    f.delivered_orders,
    f.on_time_orders,
    f.delivered_orders - f.on_time_orders           AS late_orders,
    f.on_time_delivery_rate_pct,
    -- running cumulative (window over OBT — safe in a view)
    SUM(f.on_time_orders) OVER (
        ORDER BY f.order_month
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )                                               AS cum_on_time_orders,
    SUM(f.delivered_orders) OVER (
        ORDER BY f.order_month
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )                                               AS cum_delivered_orders,
    ROUND(
        100.0 * SUM(f.on_time_orders) OVER (
            ORDER BY f.order_month
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) / NULLIF(
            SUM(f.delivered_orders) OVER (
                ORDER BY f.order_month
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
            ), 0
        ),
        4
    )                                               AS cum_on_time_delivery_rate_pct
FROM fact AS f
LEFT JOIN date_dim AS d
    ON f.order_month = d.date_day
ORDER BY f.order_month
