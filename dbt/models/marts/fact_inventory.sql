{{ config(materialized='table') }}

/*
    Fact: Current Inventory — grain = material × plant × storage location.
*/

SELECT
    sl.hk_stock_location,
    sl.hk_material,
    sl.hk_plant,
    hm.material_number,
    hp.plant_code,
    sl.storage_location,
    sl.unrestricted_stock,
    sl.quality_inspection_stock,
    sl.blocked_stock,
    CAST(sl.unrestricted_stock + sl.quality_inspection_stock + sl.blocked_stock AS DECIMAL(13, 3)) AS total_stock,
    CASE WHEN sl.unrestricted_stock <= 0 THEN TRUE ELSE FALSE END AS is_zero_stock,
    CASE WHEN sl.blocked_stock > 0 THEN TRUE ELSE FALSE END AS has_blocked_stock,
    CASE WHEN sl.quality_inspection_stock > 0 THEN TRUE ELSE FALSE END AS has_qi_stock,
    sl.record_source

FROM {{ ref('sat_stock_level') }} sl
JOIN {{ ref('hub_material') }} hm ON sl.hk_material = hm.hk_material
JOIN {{ ref('hub_plant') }} hp ON sl.hk_plant = hp.hk_plant
WHERE sl.load_date = (SELECT MAX(load_date) FROM {{ ref('sat_stock_level') }})
