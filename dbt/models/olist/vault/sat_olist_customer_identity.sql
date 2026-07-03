{{ config(
    materialized='incremental',
    unique_key='hk_olist_order',
    incremental_strategy='merge'
) }}

WITH source AS (
    SELECT
        o.order_id,
        c.customer_unique_id,
        c.customer_id         AS customer_order_key,
        NOW()                 AS load_date,
        'raw_olist.customers' AS record_source
    FROM {{ source('raw_olist', 'orders') }}    o
    JOIN {{ ref('stg_olist__customers') }}      c  ON c.customer_id = o.customer_id
    WHERE o.customer_id IS NOT NULL
),

with_hk AS (
    SELECT
        {{ dbt_utils.generate_surrogate_key(['order_id']) }}                         AS hk_olist_order,
        {{ dbt_utils.generate_surrogate_key(['order_id', 'customer_unique_id']) }}   AS hashdiff,
        customer_unique_id,
        customer_order_key,
        load_date,
        record_source
    FROM source
)

SELECT *
FROM with_hk
{% if is_incremental() %}
WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
