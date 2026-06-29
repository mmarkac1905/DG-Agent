{{ config(materialized='view') }}

/*
    Knowledge: Inventory Alerts — flagged locations needing attention.
*/

SELECT
    fi.material_number,
    m.material_description,
    m.equipment_category,
    fi.plant_code,
    p.plant_name,
    fi.storage_location,
    fi.unrestricted_stock,
    fi.blocked_stock,
    fi.total_stock,
    CASE
        WHEN fi.unrestricted_stock = 0 AND fi.total_stock = 0 THEN 'ZERO_STOCK'
        WHEN fi.blocked_stock > 0 THEN 'BLOCKED_STOCK'
        WHEN fi.unrestricted_stock > 5000 THEN 'EXCESS_STOCK'
        ELSE 'OK'
    END AS alert_type,
    CASE
        WHEN fi.unrestricted_stock = 0 AND fi.total_stock = 0 THEN 'RED'
        WHEN fi.blocked_stock > 0 THEN 'YELLOW'
        WHEN fi.unrestricted_stock > 5000 THEN 'YELLOW'
        ELSE 'GREEN'
    END AS health_status,
    CASE
        WHEN fi.unrestricted_stock = 0 AND fi.total_stock = 0
            THEN 'No stock available — check if reorder triggered'
        WHEN fi.blocked_stock > 0
            THEN 'Blocked stock present — may need quality review'
        WHEN fi.unrestricted_stock > 5000
            THEN 'Unusually high stock — check for over-ordering'
        ELSE 'Stock levels normal'
    END AS alert_message
FROM {{ ref('fact_inventory') }} fi
LEFT JOIN {{ ref('dim_material') }} m ON fi.hk_material = m.hk_material
LEFT JOIN {{ ref('dim_plant') }} p ON fi.hk_plant = p.hk_plant
WHERE fi.unrestricted_stock = 0 OR fi.blocked_stock > 0 OR fi.unrestricted_stock > 5000
