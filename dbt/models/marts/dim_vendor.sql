{{ config(materialized='table') }}

/*
    Dimension: Vendor — hub_vendor + latest sat_vendor_general + latest sat_vendor_commercial.
*/

SELECT
    h.hk_vendor,
    h.vendor_id,
    g.vendor_name,
    g.country_code,
    g.city,
    g.street_address,
    g.phone_number,
    g.created_date AS vendor_created_date,
    c.payment_terms,
    c.reconciliation_account,
    c.payment_method,
    CASE
        WHEN g.country_code = 'HR' THEN 'Domestic'
        WHEN g.country_code IN ('SI', 'AT', 'HU', 'RS', 'BA', 'ME', 'MK') THEN 'Regional (SEE)'
        WHEN g.country_code IN ('DE', 'FR', 'FI', 'IT', 'ES', 'NL', 'BE') THEN 'EU'
        WHEN g.country_code IN ('US', 'CA', 'GB') THEN 'International (West)'
        WHEN g.country_code IN ('CN', 'JP', 'KR', 'TW') THEN 'International (Asia)'
        ELSE 'Other'
    END AS vendor_region,
    h.load_date AS first_seen_date,
    h.record_source
FROM {{ ref('hub_vendor') }} h
LEFT JOIN {{ ref('sat_vendor_general') }} g
    ON h.hk_vendor = g.hk_vendor
    AND g.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_vendor_general') }} WHERE hk_vendor = h.hk_vendor)
LEFT JOIN {{ ref('sat_vendor_commercial') }} c
    ON h.hk_vendor = c.hk_vendor
    AND c.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_vendor_commercial') }} WHERE hk_vendor = h.hk_vendor)
