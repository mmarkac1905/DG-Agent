{{ config(
    materialized='view',
    tags=['olist', 'logistics', 'freight', 'obt']
) }}

SELECT
    f.product_category_english,
    f.order_month,
    dd.year                         AS order_year,
    dd.month                        AS order_month_num,
    dd.month_name                   AS order_month_name,
    dd.year_month                   AS order_year_month,
    f.total_freight_value_brl,
    f.total_item_price_brl,
    f.order_count,
    f.item_count,
    f.freight_to_price_ratio_pct
FROM {{ ref('fact_freight_to_price_ratio') }} f
LEFT JOIN {{ ref('dim_date') }} dd
    ON f.order_month = dd.date_day
ORDER BY
    f.order_month,
    f.product_category_english
