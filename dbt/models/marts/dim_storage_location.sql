{{ config(materialized='table') }}

/*
    Dimension: Storage Location — raw org structure.
*/

SELECT
    {{ hash_key(['l.WERKS', 'l.LGORT']) }} AS hk_storage_location,
    l.WERKS AS plant_code,
    l.LGORT AS storage_location_code,
    l.LGOBE AS storage_location_name,
    w.NAME1 AS plant_name,
    w.ORT01 AS plant_city,
    CASE
        WHEN l.LGOBE LIKE '%Main%' OR l.LGOBE LIKE '%Central%' THEN 'primary'
        WHEN l.LGOBE LIKE '%Defective%' OR l.LGOBE LIKE '%Return%' THEN 'returns'
        WHEN l.LGOBE LIKE '%Ready%' OR l.LGOBE LIKE '%Deploy%' THEN 'staging'
        WHEN l.LGOBE LIKE '%Regional%' THEN 'regional'
        ELSE 'other'
    END AS location_type
FROM {{ source('raw_sap', 't001l') }} l
LEFT JOIN {{ source('raw_sap', 't001w') }} w ON l.WERKS = w.WERKS
