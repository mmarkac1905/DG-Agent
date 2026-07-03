{{ config(materialized='incremental', unique_key='hk_olist_order', on_schema_change='sync_all_columns') }}

WITH src AS (
    SELECT
        {{ dbt_utils.generate_surrogate_key(['order_id']) }}              AS hk_olist_order,
        {{ dbt_utils.generate_surrogate_key(['order_status',
            'order_purchase_timestamp']) }}                                AS hashdiff,
        order_status,
        order_purchase_timestamp,
        order_approved_at,
        order_delivered_carrier_date,
        order_delivered_customer_date,
        order_estimated_delivery_date,
        order_purchase_timestamp                                           AS load_date,
        'raw_olist.orders'                                                 AS record_source
    FROM {{ ref('stg_olist__orders') }}
)

SELECT *
FROM src
{% if is_incremental() %}
    WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
