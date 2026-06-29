{{ config(materialized='table') }}

/*
    Dimension: Customer — hub_customer + latest sat_customer.
*/

SELECT
    h.hk_customer,
    h.customer_id,
    c.customer_name,
    c.country,
    c.city,
    c.postal_code,
    c.account_group,
    CASE WHEN c.account_group = '0002' THEN 'Business' ELSE 'Residential' END AS customer_segment,
    c.created_date AS customer_created_date,
    h.load_date AS first_seen_date,
    h.record_source
FROM {{ ref('hub_customer') }} h
LEFT JOIN {{ ref('sat_customer') }} c
    ON h.hk_customer = c.hk_customer
    AND c.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_customer') }} WHERE hk_customer = h.hk_customer)
