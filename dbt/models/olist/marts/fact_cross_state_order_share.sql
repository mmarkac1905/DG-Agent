{{ config(materialized='table') }}

/*
  BG035 — Cross-State Order Share
  Grain: one row per month of order purchase
  Numerator:   distinct delivered orders where any item has seller_state <> customer_state
  Denominator: all distinct delivered orders

  Join path:
    hub_olist_order
    → sat_olist_order_header        (order_status, order_purchase_timestamp)
    → sat_olist_customer_location   (customer_state)                          [new sat]
    → link_olist_order_item         (order_id, order_item_id, seller_id)
    → sat_olist_seller_geo          (seller_state)                            [new sat via hub_olist_seller]

  Cardinality notes (DIRECTIVE 3g):
    - hub_olist_order ↔ sat_olist_order_header : SCD2 deduped to current → 1:1
    - hub_olist_order ↔ sat_olist_customer_location : 1:1 (DAR-01046 per_record_key)
    - hub_olist_order ↔ link_olist_order_item : 1:N (DAR-01055 header_detail avg 1.14x)
      → collapsed safely by COUNT(DISTINCT)
    - link_olist_order_item ↔ hub_olist_seller : N:1 lookup
      → DAR-01057 catastrophic_fanout applies to seller-driving direction only;
         we drive order_items → seller (lookup), which is safe.
*/

WITH order_header AS (
    -- Current version of each order header (SCD2 dedup)
    SELECT
        h.order_id,
        h.hk_olist_order,
        s.order_status,
        s.order_purchase_timestamp
    FROM {{ ref('hub_olist_order') }}        h
    JOIN {{ ref('sat_olist_order_header') }} s
        ON s.hk_olist_order = h.hk_olist_order
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY s.hk_olist_order
        ORDER BY s.load_date DESC
    ) = 1
),

delivered AS (
    -- Filter to delivered orders only.
    -- DAR-01021: 96,478 delivered / 99,441 total (97.0%).
    -- TAR-00141 reflection confirmed order_status='delivered' is the correct sole filter.
    SELECT
        hk_olist_order,
        order_id,
        DATE_TRUNC('month', order_purchase_timestamp)::DATE AS order_month
    FROM order_header
    WHERE order_status = 'delivered'
),

customer_state AS (
    -- Current version of customer location satellite (1:1 to order per DAR-01046)
    SELECT
        cl.hk_olist_order,
        cl.customer_state
    FROM {{ ref('sat_olist_customer_location') }} cl
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY cl.hk_olist_order
        ORDER BY cl.load_date DESC
    ) = 1
),

seller_geo AS (
    -- Dedupe the satellite to its current version BEFORE joining the
    -- link: qualifying the joined set by seller would keep one ITEM per
    -- seller and silently drop the rest (analyst-verified repair).
    SELECT hk_olist_seller, seller_state
    FROM {{ ref('sat_olist_seller_geo') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY hk_olist_seller
        ORDER BY load_date DESC
    ) = 1
),

order_items AS (
    -- link carries seller_id; we need seller_state from the new satellite.
    -- Driving direction: order → item → seller (lookup). Safe per DIRECTIVE 3g.
    SELECT
        li.hk_olist_order,
        li.order_id,
        li.seller_id,
        sg.seller_state
    FROM {{ ref('link_olist_order_item') }}   li
    JOIN {{ ref('hub_olist_seller') }}         hs
        ON hs.seller_id = li.seller_id
    JOIN seller_geo                            sg
        ON sg.hk_olist_seller = hs.hk_olist_seller
    -- seller_state null_pct=0 (DAR-01061); no COALESCE needed
),

cross_state_per_order AS (
    -- Flag each delivered order as cross-state (1) or same-state (0).
    -- An order is cross-state if ANY item has seller_state <> customer_state.
    -- TAR-00140 confirmed null_state_orders = 0, so no null guard required.
    SELECT
        d.order_id,
        d.order_month,
        MAX(
            CASE WHEN oi.seller_state <> cs.customer_state THEN 1 ELSE 0 END
        ) AS is_cross_state
    FROM delivered                   d
    JOIN customer_state              cs ON cs.hk_olist_order = d.hk_olist_order
    LEFT JOIN order_items            oi ON oi.order_id       = d.order_id
    GROUP BY d.order_id, d.order_month
),

monthly AS (
    SELECT
        order_month,
        COUNT(DISTINCT order_id)                                        AS delivered_orders,
        COUNT(DISTINCT CASE WHEN is_cross_state = 1 THEN order_id END) AS cross_state_orders,
        COUNT(DISTINCT CASE WHEN is_cross_state = 0 THEN order_id END) AS same_state_orders
    FROM cross_state_per_order
    GROUP BY order_month
)

SELECT
    order_month,
    delivered_orders,
    cross_state_orders,
    same_state_orders,
    ROUND(
        cross_state_orders * 100.0 / NULLIF(delivered_orders, 0),
        4
    )                                                                  AS cross_state_order_share_pct
FROM monthly
ORDER BY order_month
