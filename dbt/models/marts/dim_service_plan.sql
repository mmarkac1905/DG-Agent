{{ config(materialized='table') }}

/*
    Dimension: Service Plan — hub_service_plan + latest sat_service_plan.
*/

SELECT
    h.hk_service_plan,
    h.service_plan_id,
    s.service_plan_name,
    s.service_plan_group,
    CASE
        WHEN s.service_plan_group = 'SVC-FIB' THEN 'Fiber Broadband'
        WHEN s.service_plan_group = 'SVC-TV'  THEN 'IPTV Bundle'
        WHEN s.service_plan_group = 'SVC-CBL' THEN 'Cable Broadband'
        WHEN s.service_plan_group = 'SVC-BIZ' THEN 'Business Connectivity'
        ELSE 'Other'
    END AS plan_category,
    h.load_date AS first_seen_date,
    h.record_source
FROM {{ ref('hub_service_plan') }} h
LEFT JOIN {{ ref('sat_service_plan') }} s
    ON h.hk_service_plan = s.hk_service_plan
    AND s.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_service_plan') }} WHERE hk_service_plan = h.hk_service_plan)
