{{ config(materialized='view') }}

/*
    OBT: Inventory Health — current stock with material, plant, storage location context.
*/

SELECT
    fi.material_number,
    m.material_description,
    m.equipment_category,
    m.material_group,
    fi.plant_code,
    p.plant_name,
    p.plant_city,
    fi.storage_location,
    sl.storage_location_name,
    sl.location_type,
    fi.unrestricted_stock,
    fi.quality_inspection_stock,
    fi.blocked_stock,
    fi.total_stock,
    fi.is_zero_stock,
    fi.has_blocked_stock,
    fi.has_qi_stock
FROM {{ ref('fact_inventory') }} fi
LEFT JOIN {{ ref('dim_material') }} m ON fi.hk_material = m.hk_material
LEFT JOIN {{ ref('dim_plant') }} p ON fi.hk_plant = p.hk_plant
LEFT JOIN {{ ref('dim_storage_location') }} sl
    ON fi.plant_code = sl.plant_code AND fi.storage_location = sl.storage_location_code
