{{ config(materialized='incremental', unique_key='hk_olist_order', on_schema_change='sync_all_columns') }}

WITH src AS (
    SELECT
        order_id,
        order_purchase_timestamp AS load_date,
        'raw_olist.orders'        AS record_source
    FROM {{ ref('stg_olist__orders') }}
)

SELECT
    {{ dbt_utils.generate_surrogate_key(['order_id']) }} AS hk_olist_order,
    order_id,
    load_date,
    record_source
FROM src
{% if is_incremental() %}
    WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
