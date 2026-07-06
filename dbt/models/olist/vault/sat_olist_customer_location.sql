{{ config(
    materialized='incremental',
    unique_key='hk_olist_order'
) }}

WITH src AS (
    SELECT
        {{ dbt_utils.generate_surrogate_key(['o.order_id']) }}   AS hk_olist_order,
        MD5(COALESCE(c.customer_state, '')    || '|' ||
            COALESCE(c.customer_city, '')     || '|' ||
            COALESCE(c.customer_zip_code_prefix, ''))            AS hashdiff,
        c.customer_state,
        c.customer_city,
        c.customer_zip_code_prefix,
        CURRENT_TIMESTAMP                                        AS load_date,
        'raw_olist.customers'                                    AS record_source
    FROM {{ ref('stg_olist__orders') }}    o
    JOIN {{ ref('stg_olist__customers') }} c
        ON c.customer_id = o.customer_id
    WHERE o.order_id IS NOT NULL
),
deduped AS (
    SELECT *
    FROM src
    QUALIFY ROW_NUMBER() OVER (PARTITION BY hk_olist_order ORDER BY load_date DESC) = 1
)
SELECT
    hk_olist_order,
    hashdiff,
    customer_state,
    customer_city,
    customer_zip_code_prefix,
    load_date,
    record_source
FROM deduped
{% if is_incremental() %}
WHERE load_date > (SELECT MAX(load_date) FROM {{ this }})
{% endif %}
