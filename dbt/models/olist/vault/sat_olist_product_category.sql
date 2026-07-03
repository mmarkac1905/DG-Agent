{{ config(materialized='incremental', unique_key='hk_olist_product', on_schema_change='sync_all_columns') }}

WITH products AS (
    SELECT
        product_id,
        product_category_name,
        product_weight_g,
        product_photos_qty
    FROM {{ ref('stg_olist__products') }}
),

category AS (
    SELECT
        product_category_name,
        product_category_name_english
    FROM {{ ref('stg_olist__category_translation') }}
),

enriched AS (
    SELECT
        p.product_id,
        p.product_category_name                                        AS product_category_name_pt,
        COALESCE(ct.product_category_name_english, 'uncategorized')    AS product_category_english,
        p.product_weight_g,
        p.product_photos_qty
    FROM products AS p
    LEFT JOIN category AS ct
        ON p.product_category_name = ct.product_category_name
)

SELECT
    {{ dbt_utils.generate_surrogate_key(['product_id']) }}              AS hk_olist_product,
    {{ dbt_utils.generate_surrogate_key(['product_category_name_pt',
        'product_category_english']) }}                                  AS hashdiff,
    product_category_name_pt,
    product_category_english,
    product_weight_g,
    product_photos_qty,
    CURRENT_TIMESTAMP                                                    AS load_date,
    'raw_olist.products+category_translation'                            AS record_source
FROM enriched
{% if is_incremental() %}
    WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
