{{ config(
    materialized='table',
    tags=['ecommerce', 'delivery', 'monthly']
) }}

WITH hub AS (
    SELECT
        hk_olist_order,
        order_id
    FROM {{ ref('hub_olist_order') }}
),

sat_current AS (
    SELECT
        hk_olist_order,
        order_status,
        order_purchase_timestamp,
        order_delivered_customer_date,
        order_estimated_delivery_date
    FROM {{ ref('sat_olist_order_header') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY hk_olist_order
        ORDER BY load_date DESC
    ) = 1
),

joined AS (
    SELECT
        h.order_id,
        s.order_status,
        s.order_purchase_timestamp,
        s.order_delivered_customer_date,
        s.order_estimated_delivery_date
    FROM hub AS h
    INNER JOIN sat_current AS s
        ON h.hk_olist_order = s.hk_olist_order
),

eligible AS (
    SELECT
        order_id,
        DATE_TRUNC('month', order_purchase_timestamp) AS order_month,
        CASE
            WHEN order_delivered_customer_date <= order_estimated_delivery_date THEN 1
            ELSE 0
        END AS is_on_time
    FROM joined
    WHERE
        order_status = 'delivered'
        AND order_delivered_customer_date IS NOT NULL
)

SELECT
    order_month,
    COUNT(*)                                                AS delivered_orders,
    SUM(is_on_time)                                        AS on_time_orders,
    ROUND(
        100.0 * SUM(is_on_time) / NULLIF(COUNT(*), 0),
        4
    )                                                      AS on_time_delivery_rate_pct
FROM eligible
GROUP BY order_month
ORDER BY order_month
