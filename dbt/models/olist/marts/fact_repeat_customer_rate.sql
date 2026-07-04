{{ config(materialized='table') }}

WITH order_header_all AS (
    -- All orders regardless of status, used ONLY for computing each person's first order timestamp
    SELECT
        h.order_id,
        h.hk_olist_order,
        s.order_status,
        s.order_purchase_timestamp
    FROM {{ ref('hub_olist_order') }}          h
    JOIN {{ ref('sat_olist_order_header') }}   s  ON s.hk_olist_order = h.hk_olist_order
    QUALIFY ROW_NUMBER() OVER (PARTITION BY s.hk_olist_order ORDER BY s.load_date DESC) = 1
),

order_header AS (
    SELECT
        order_id,
        hk_olist_order,
        order_status,
        order_purchase_timestamp
    FROM order_header_all
    -- Exclude only canceled per DAR-00973 / known_issue #134
    WHERE order_status != 'canceled'
),

customer_identity AS (
    SELECT
        hk_olist_order,
        customer_unique_id
    FROM {{ ref('sat_olist_customer_identity') }}
    QUALIFY ROW_NUMBER() OVER (PARTITION BY hk_olist_order ORDER BY load_date DESC) = 1
),

-- All orders (no status filter) joined to customer identity for first-order baseline
all_orders_with_person AS (
    SELECT
        o.order_id,
        o.hk_olist_order,
        o.order_purchase_timestamp,
        ci.customer_unique_id
    FROM order_header_all   o
    JOIN customer_identity  ci ON ci.hk_olist_order = o.hk_olist_order
),

-- In-scope orders (canceled excluded) joined to customer identity for counting
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

-- Determine each person's earliest order timestamp across ALL orders (any status)
person_first_order AS (
    SELECT
        customer_unique_id,
        MIN(order_purchase_timestamp) AS first_order_ts
    FROM all_orders_with_person
    GROUP BY customer_unique_id
),

-- Flag each in-scope order as repeat (person had at least one earlier order, any status)
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
