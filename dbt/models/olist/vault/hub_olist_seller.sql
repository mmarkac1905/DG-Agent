{{ config(
    materialized='incremental',
    unique_key='hk_olist_seller'
) }}

WITH src AS (
    SELECT
        seller_id,
        {{ dbt_utils.generate_surrogate_key(['seller_id']) }} AS hk_olist_seller,
        CURRENT_TIMESTAMP                AS load_date,
        'raw_olist.sellers'              AS record_source
    FROM {{ ref('stg_olist__sellers') }}
    WHERE seller_id IS NOT NULL
)
SELECT
    hk_olist_seller,
    seller_id,
    load_date,
    record_source
FROM src
{% if is_incremental() %}
WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
