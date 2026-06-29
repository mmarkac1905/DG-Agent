{{ config(materialized='table') }}

/*
    Dimension: Plant — hub_plant + raw org structure reference.
*/

SELECT
    h.hk_plant,
    h.plant_code,
    w.NAME1 AS plant_name,
    w.ORT01 AS plant_city,
    w.LAND1 AS plant_country,
    t.BUTXT AS company_name,
    w.BUKRS AS company_code,
    h.load_date AS first_seen_date,
    h.record_source
FROM {{ ref('hub_plant') }} h
LEFT JOIN {{ source('raw_sap', 't001w') }} w ON h.plant_code = w.WERKS
LEFT JOIN {{ source('raw_sap', 't001') }} t ON w.BUKRS = t.BUKRS
