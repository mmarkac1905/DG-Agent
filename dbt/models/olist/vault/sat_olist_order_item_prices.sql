{{ config(materialized='incremental', unique_key='hk_olist_order_item', on_schema_change='sync_all_columns') }}

WITH src AS (
    SELECT
        {{ dbt_utils.generate_surrogate_key(['order_id', 'order_item_id']) }} AS hk_olist_order_item,
        {{ dbt_utils.generate_surrogate_key(['price', 'freight_value']) }}     AS hashdiff,
        price,
        freight_value,
        shipping_limit_date                                                    AS load_date,
        'raw_olist.order_items'                                                AS record_source
    FROM {{ ref('stg_olist__order_items') }}
)

SELECT *
FROM src
{% if is_incremental() %}
    WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
