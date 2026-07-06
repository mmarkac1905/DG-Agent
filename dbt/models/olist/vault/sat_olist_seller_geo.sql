{{ config(
    materialized='incremental',
    unique_key='hk_olist_seller'
) }}

WITH src AS (
    SELECT
        {{ dbt_utils.generate_surrogate_key(['seller_id']) }}    AS hk_olist_seller,
        MD5(COALESCE(seller_state, '') || '|' ||
            COALESCE(seller_city, '')  || '|' ||
            COALESCE(seller_zip_code_prefix, ''))                AS hashdiff,
        seller_state,
        seller_city,
        seller_zip_code_prefix,
        CURRENT_TIMESTAMP                                        AS load_date,
        'raw_olist.sellers'                                      AS record_source
    FROM {{ ref('stg_olist__sellers') }}
    WHERE seller_id IS NOT NULL
),
deduped AS (
    SELECT *
    FROM src
    QUALIFY ROW_NUMBER() OVER (PARTITION BY hk_olist_seller ORDER BY load_date DESC) = 1
)
SELECT
    hk_olist_seller,
    hashdiff,
    seller_state,
    seller_city,
    seller_zip_code_prefix,
    load_date,
    record_source
FROM deduped
{% if is_incremental() %}
WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
