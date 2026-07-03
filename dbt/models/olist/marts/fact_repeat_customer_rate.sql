{{ config(materialized='table') }}

WITH order_header AS (
    SELECT
        h.order_id,
        h.hk_olist_order,
        s.order_status,
        s.order_purchase_timestamp
    FROM {{ ref('hub_olist_order') }}          h
    JOIN {{ ref('sat_olist_order_header') }}   s  ON s.hk_olist_order = h.hk_olist_order
    -- Exclude canceled (625 rows) and unavailable (609 rows) per DAR-00973;
    -- all remaining statuses (delivered, shipped, invoiced, processing, created, approved)
    -- represent orders that were placed.
    WHERE s.order_status NOT IN ('canceled', 'unavailable')
    QUALIFY ROW_NUMBER() OVER (PARTITION BY s.hk_olist_order ORDER BY s.load_date DESC) = 1
),

customer_identity AS (
    SELECT
        hk_olist_order,
        customer_unique_id
    FROM {{ ref('sat_olist_customer_identity') }}
    QUALIFY ROW_NUMBER() OVER (PARTITION BY hk_olist_order ORDER BY load_date DESC) = 1
),

orders_with_person AS (
    SELECT
        o.order_id,
        o.hk_olist_order,
        o.order_status,
        o.order_purchase_timestamp,
        DATE_TRUNC('month', o.order_purchase_timestamp)::DATE  AS order_month,
        ci.customer_unique_id
    FROM order_header       o
    JOIN customer_identity  ci ON ci.hk_olist_order = o.hk_olist_order
),

-- Determine each person's earliest order timestamp across all in-scope orders
person_first_order AS (
    SELECT
        customer_unique_id,
        MIN(order_purchase_timestamp) AS first_order_ts
    FROM orders_with_person
    GROUP BY customer_unique_id
),

-- Flag each order as repeat (person had at least one earlier order)
orders_classified AS (
    SELECT
        o.order_id,
        o.order_month,
        o.customer_unique_id,
        CASE
            WHEN o.order_purchase_timestamp > p.first_order_ts THEN TRUE
            ELSE FALSE
        END AS is_repeat_order
    FROM orders_with_person  o
    JOIN person_first_order  p ON p.customer_unique_id = o.customer_unique_id
)

SELECT
    order_month,
    COUNT(DISTINCT order_id)                                              AS total_orders,
    COUNT(DISTINCT CASE WHEN is_repeat_order THEN order_id END)           AS repeat_orders,
    ROUND(
        100.0
        * COUNT(DISTINCT CASE WHEN is_repeat_order THEN order_id END)
        / NULLIF(COUNT(DISTINCT order_id), 0)
    , 2)                                                                  AS repeat_customer_rate_pct
FROM orders_classified
GROUP BY order_month
ORDER BY order_month
