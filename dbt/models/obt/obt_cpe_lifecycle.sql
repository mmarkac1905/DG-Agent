{{ config(materialized='view') }}

/*
    OBT: CPE Lifecycle — equipment with material context.
*/

SELECT
    e.equipment_number,
    e.serial_number,
    e.manufacturer,
    e.model_description,
    e.lifecycle_status,
    e.current_status,
    e.status_since,
    e.received_date,
    e.startup_date,
    e.current_plant,
    e.days_since_receipt,
    e.days_since_deployment,
    m.material_description,
    m.material_group,
    m.equipment_category,
    m.gross_weight_kg
FROM {{ ref('dim_equipment') }} e
LEFT JOIN {{ ref('dim_material') }} m ON e.material_number = m.material_number
