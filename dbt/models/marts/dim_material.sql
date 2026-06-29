{{ config(materialized='table') }}

/*
    Dimension: Material (CPE type) — hub_material + latest general + latest description.
*/

SELECT
    h.hk_material,
    h.material_number,
    d.material_description,
    g.material_type,
    g.material_group,
    g.base_unit_of_measure,
    g.gross_weight_kg,
    g.weight_unit,
    g.cross_plant_status,
    g.division,
    g.product_hierarchy,
    g.created_date AS material_created_date,
    CASE
        WHEN g.material_group = 'CPE-RTR' THEN 'Router'
        WHEN g.material_group = 'CPE-ONT' THEN 'ONT'
        WHEN g.material_group = 'CPE-STB' THEN 'Set-Top Box'
        WHEN g.material_group = 'CPE-SWT' THEN 'Switch'
        WHEN g.material_group = 'CPE-MDM' THEN 'Modem'
        ELSE 'Other'
    END AS equipment_category,
    h.load_date AS first_seen_date,
    h.record_source
FROM {{ ref('hub_material') }} h
LEFT JOIN {{ ref('sat_material_general') }} g
    ON h.hk_material = g.hk_material
    AND g.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_material_general') }} WHERE hk_material = h.hk_material)
LEFT JOIN {{ ref('sat_material_description') }} d
    ON h.hk_material = d.hk_material
    AND d.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_material_description') }} WHERE hk_material = h.hk_material)
