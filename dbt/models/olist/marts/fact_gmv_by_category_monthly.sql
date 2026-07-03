{{ config(materialized='table') }}

WITH order_header AS (
    -- Deduplicate SCD2 satellite to current version
    SELECT
        hk_olist_order,
        order_status,
        order_purchase_timestamp,
        DATE_TRUNC('month', order_purchase_timestamp) AS order_month
    FROM (
        SELECT
            hk_olist_order,
            order_status,
            order_purchase_timestamp,
            ROW_NUMBER() OVER (
                PARTITION BY hk_olist_order
                ORDER BY load_date DESC
            ) AS rn
        FROM {{ ref('sat_olist_order_header') }}
    ) AS ranked
    WHERE rn = 1
      AND order_status != 'canceled'
),

order_item_link AS (
    SELECT
        hk_olist_order_item,
        hk_olist_order,
        hk_olist_product,
        order_id,
        order_item_id
    FROM {{ ref('link_olist_order_item') }}
),

item_prices AS (
    -- Deduplicate SCD2 satellite to current version
    SELECT
        hk_olist_order_item,
        price,
        freight_value
    FROM (
        SELECT
            hk_olist_order_item,
            price,
            freight_value,
            ROW_NUMBER() OVER (
                PARTITION BY hk_olist_order_item
                ORDER BY load_date DESC
            ) AS rn
        FROM {{ ref('sat_olist_order_item_prices') }}
    ) AS ranked
    WHERE rn = 1
),

product_category AS (
    -- Deduplicate SCD2 satellite to current version
    SELECT
        hk_olist_product,
        product_category_english
    FROM (
        SELECT
            hk_olist_product,
            product_category_english,
            ROW_NUMBER() OVER (
                PARTITION BY hk_olist_product
                ORDER BY load_date DESC
            ) AS rn
        FROM {{ ref('sat_olist_product_category') }}
    ) AS ranked
    WHERE rn = 1
),

joined AS (
    SELECT
        oh.order_month,
        pc.product_category_english,
        ip.price,
        oil.order_id
    FROM order_item_link           AS oil
    INNER JOIN order_header        AS oh  ON oil.hk_olist_order   = oh.hk_olist_order
    INNER JOIN item_prices         AS ip  ON oil.hk_olist_order_item = ip.hk_olist_order_item
    INNER JOIN product_category    AS pc  ON oil.hk_olist_product  = pc.hk_olist_product
)

SELECT
    product_category_english,
    order_month,
    SUM(price)               AS gmv_brl,
    COUNT(DISTINCT order_id) AS order_count,
    COUNT(*)                 AS item_count
FROM joined
GROUP BY
    product_category_english,
    order_month
ORDER BY
    order_month,
    product_category_english
