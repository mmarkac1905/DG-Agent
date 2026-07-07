{{ config(
    materialized='table',
    tags=['olist', 'logistics', 'freight']
) }}

WITH delivered_orders AS (
    SELECT
        h.hk_olist_order,
        h.order_id,
        DATE_TRUNC('month', soh.order_purchase_timestamp)::DATE AS order_month
    FROM {{ ref('hub_olist_order') }} h
    INNER JOIN {{ ref('sat_olist_order_header') }} soh
        ON h.hk_olist_order = soh.hk_olist_order
    WHERE soh.order_status = 'delivered'
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY soh.hk_olist_order
        ORDER BY soh.load_date DESC
    ) = 1
),

order_items_linked AS (
    -- header_detail join; safe fan-out (avg 1.14x per DAR-01055)
    SELECT
        loi.hk_olist_order_item,
        loi.hk_olist_order,
        loi.hk_olist_product
    FROM {{ ref('link_olist_order_item') }} loi
    INNER JOIN delivered_orders d
        ON loi.hk_olist_order = d.hk_olist_order
),

item_prices AS (
    SELECT
        oil.hk_olist_order_item,
        oil.hk_olist_order,
        oil.hk_olist_product,
        sip.price,
        sip.freight_value
    FROM order_items_linked oil
    INNER JOIN {{ ref('sat_olist_order_item_prices') }} sip
        ON oil.hk_olist_order_item = sip.hk_olist_order_item
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY sip.hk_olist_order_item
        ORDER BY sip.load_date DESC
    ) = 1
),

product_categories AS (
    -- safe direction: product -> category (reverse is catastrophic_fanout per DAR-01042)
    -- sat_olist_product_category stores product_category_english natively
    SELECT
        hp.hk_olist_product,
        COALESCE(spc.product_category_english, 'uncategorized') AS product_category_english
    FROM {{ ref('hub_olist_product') }} hp
    INNER JOIN {{ ref('sat_olist_product_category') }} spc
        ON hp.hk_olist_product = spc.hk_olist_product
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY spc.hk_olist_product
        ORDER BY spc.load_date DESC
    ) = 1
),

enriched AS (
    SELECT
        ip.hk_olist_order_item,
        ip.hk_olist_order,
        d.order_month,
        COALESCE(pc.product_category_english, 'uncategorized') AS product_category_english,
        ip.price,
        ip.freight_value
    FROM item_prices ip
    INNER JOIN delivered_orders d
        ON ip.hk_olist_order = d.hk_olist_order
    LEFT JOIN product_categories pc
        ON ip.hk_olist_product = pc.hk_olist_product
)

SELECT
    product_category_english,
    order_month,
    SUM(freight_value)                                              AS total_freight_value_brl,
    SUM(price)                                                      AS total_item_price_brl,
    COUNT(DISTINCT hk_olist_order)                                  AS order_count,
    COUNT(hk_olist_order_item)                                      AS item_count,
    ROUND(
        100.0 * SUM(freight_value) / NULLIF(SUM(price), 0),
        4
    )                                                               AS freight_to_price_ratio_pct
FROM enriched
GROUP BY
    product_category_english,
    order_month
