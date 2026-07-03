{{ config(materialized='view') }}

SELECT
    product_category_english,
    order_month,
    STRFTIME(order_month, '%Y-%m')          AS order_month_label,
    EXTRACT(YEAR  FROM order_month)::INT     AS order_year,
    EXTRACT(MONTH FROM order_month)::INT     AS order_month_num,
    gmv_brl,
    order_count,
    item_count,
    ROUND(gmv_brl / NULLIF(order_count, 0), 2) AS avg_gmv_per_order,
    ROUND(gmv_brl / NULLIF(item_count,  0), 2) AS avg_item_price
FROM {{ ref('fact_gmv_by_category_monthly') }}
