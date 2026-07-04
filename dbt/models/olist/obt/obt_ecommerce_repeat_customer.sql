{{ config(materialized='view') }}

-- Date attributes derived inline: the Olist folder must stay standalone
-- (no cross-source ref into the SAP mart layer's dim_date).
SELECT
    f.order_month,
    DATE_PART('year', f.order_month)::INT          AS year,
    DATE_PART('month', f.order_month)::INT         AS month,
    STRFTIME(f.order_month, '%B')                  AS month_name,
    STRFTIME(f.order_month, '%Y-%m')               AS year_month,
    DATE_PART('quarter', f.order_month)::INT       AS quarter,
    STRFTIME(f.order_month, '%Y') || '-Q' ||
        DATE_PART('quarter', f.order_month)::INT   AS year_quarter,
    f.total_orders,
    f.repeat_orders,
    f.total_orders - f.repeat_orders               AS first_time_orders,
    f.repeat_customer_rate_pct
FROM {{ ref('fact_repeat_customer_rate') }}  f
ORDER BY f.order_month
