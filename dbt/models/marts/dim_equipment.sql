{{ config(materialized='table') }}

/*
    Dimension: Equipment — current state per CPE device.
    Links: hub_equipment + latest sat_equipment_general + latest sat_equipment_status + link_equipment_material.
*/

WITH latest_status AS (
    SELECT
        hk_equipment,
        status_code,
        status_description,
        status_from_date,
        ROW_NUMBER() OVER (PARTITION BY hk_equipment ORDER BY status_from_date DESC) AS rn
    FROM {{ ref('sat_equipment_status') }}
)

SELECT
    h.hk_equipment,
    h.equipment_number,
    g.serial_number,
    g.manufacturer,
    g.model_description,
    g.equipment_category,
    g.plant AS current_plant,
    g.startup_date,
    g.created_date AS received_date,
    ls.status_code AS current_status_code,
    ls.status_description AS current_status,
    ls.status_from_date AS status_since,
    CASE
        WHEN ls.status_code = 'INST' THEN 'deployed'
        WHEN ls.status_code = 'AVLB' THEN 'in_stock'
        WHEN ls.status_code = 'RET' THEN 'returned'
        WHEN ls.status_code = 'DLFL' THEN 'defective'
        ELSE 'unknown'
    END AS lifecycle_status,
    lm.material_number,
    CASE WHEN g.created_date IS NOT NULL
        THEN CAST(CURRENT_DATE - g.created_date AS INTEGER)
        ELSE NULL
    END AS days_since_receipt,
    CASE WHEN g.startup_date IS NOT NULL
        THEN CAST(CURRENT_DATE - g.startup_date AS INTEGER)
        ELSE NULL
    END AS days_since_deployment,
    h.load_date AS first_seen_date,
    h.record_source
FROM {{ ref('hub_equipment') }} h
LEFT JOIN {{ ref('sat_equipment_general') }} g
    ON h.hk_equipment = g.hk_equipment
    AND g.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_equipment_general') }} WHERE hk_equipment = h.hk_equipment)
LEFT JOIN latest_status ls
    ON h.hk_equipment = ls.hk_equipment AND ls.rn = 1
LEFT JOIN {{ ref('link_equipment_material') }} lm
    ON h.hk_equipment = lm.hk_equipment
