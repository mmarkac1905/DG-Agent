{{ config(materialized='incremental', unique_key='hk_olist_product', on_schema_change='sync_all_columns') }}

WITH src AS (
    SELECT
        product_id,
        CURRENT_TIMESTAMP         AS load_date,
        'raw_olist.products'      AS record_source
    FROM {{ ref('stg_olist__products') }}
)

SELECT
    {{ dbt_utils.generate_surrogate_key(['product_id']) }} AS hk_olist_product,
    product_id,
    load_date,
    record_source
FROM src
{% if is_incremental() %}
    WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
