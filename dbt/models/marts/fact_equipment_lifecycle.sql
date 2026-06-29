{{ config(materialized='table') }}

/*
    Fact: Equipment Lifecycle Events — grain = equipment × status change.
    Enables CPE defect rate (BG003) and lifecycle status (BG006).
*/

WITH status_history AS (
    SELECT
        hk_equipment,
        status_code,
        status_description,
        status_from_date,
        LEAD(status_from_date) OVER (
            PARTITION BY hk_equipment ORDER BY status_from_date
        ) AS status_to_date,
        ROW_NUMBER() OVER (
            PARTITION BY hk_equipment ORDER BY status_from_date
        ) AS status_sequence,
        ROW_NUMBER() OVER (
            PARTITION BY hk_equipment ORDER BY status_from_date DESC
        ) AS reverse_sequence
    FROM {{ ref('sat_equipment_status') }}
)

SELECT
    sh.hk_equipment,
    he.equipment_number,
    eg.serial_number,
    eg.manufacturer,
    eg.model_description,
    eg.plant,
    lem.material_number,

    sh.status_code,
    sh.status_description,
    sh.status_from_date,
    sh.status_to_date,
    sh.status_sequence,

    CASE
        WHEN sh.status_code = 'INST' THEN 'deployed'
        WHEN sh.status_code = 'AVLB' THEN 'in_stock'
        WHEN sh.status_code = 'RET' THEN 'returned'
        WHEN sh.status_code = 'DLFL' THEN 'defective'
        ELSE 'unknown'
    END AS lifecycle_status,

    CASE WHEN sh.status_to_date IS NOT NULL
        THEN CAST(sh.status_to_date - sh.status_from_date AS INTEGER)
        ELSE CAST(CURRENT_DATE - sh.status_from_date AS INTEGER)
    END AS days_in_status,

    sh.reverse_sequence = 1 AS is_current_status,

    CASE WHEN sh.status_code = 'INST'
        THEN CAST(sh.status_from_date - eg.created_date AS INTEGER)
        ELSE NULL
    END AS days_receipt_to_deployment,

    eg.record_source

FROM status_history sh
JOIN {{ ref('hub_equipment') }} he ON sh.hk_equipment = he.hk_equipment
LEFT JOIN {{ ref('sat_equipment_general') }} eg
    ON sh.hk_equipment = eg.hk_equipment
    AND eg.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_equipment_general') }} WHERE hk_equipment = sh.hk_equipment)
LEFT JOIN {{ ref('link_equipment_material') }} lem ON sh.hk_equipment = lem.hk_equipment
