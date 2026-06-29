{{ config(materialized='view') }}

/*
    Knowledge: CPE Lifecycle Metrics per equipment category.
*/

SELECT
    m.material_group,
    CASE
        WHEN m.material_group = 'CPE-RTR' THEN 'Router'
        WHEN m.material_group = 'CPE-ONT' THEN 'ONT'
        WHEN m.material_group = 'CPE-STB' THEN 'Set-Top Box'
        WHEN m.material_group = 'CPE-SWT' THEN 'Switch'
        WHEN m.material_group = 'CPE-MDM' THEN 'Modem'
        ELSE 'Other'
    END AS equipment_category,
    COUNT(*) AS total_devices,
    COUNT(CASE WHEN e.lifecycle_status = 'deployed' THEN 1 END) AS deployed,
    COUNT(CASE WHEN e.lifecycle_status = 'in_stock' THEN 1 END) AS in_stock,
    COUNT(CASE WHEN e.lifecycle_status = 'returned' THEN 1 END) AS returned,
    COUNT(CASE WHEN e.lifecycle_status = 'defective' THEN 1 END) AS defective,
    ROUND(100.0 * COUNT(CASE WHEN e.lifecycle_status = 'deployed' THEN 1 END) / COUNT(*), 1) AS deployment_rate_pct,
    ROUND(100.0 * COUNT(CASE WHEN e.lifecycle_status = 'defective' THEN 1 END) / COUNT(*), 2) AS defect_rate_pct,
    ROUND(100.0 * COUNT(CASE WHEN e.lifecycle_status = 'returned' THEN 1 END) / COUNT(*), 2) AS return_rate_pct,
    CASE
        WHEN 100.0 * COUNT(CASE WHEN e.lifecycle_status = 'defective' THEN 1 END) / COUNT(*) > 5 THEN 'RED'
        WHEN 100.0 * COUNT(CASE WHEN e.lifecycle_status = 'defective' THEN 1 END) / COUNT(*) > 3 THEN 'YELLOW'
        ELSE 'GREEN'
    END AS defect_health,
    CASE
        WHEN 100.0 * COUNT(CASE WHEN e.lifecycle_status = 'returned' THEN 1 END) / COUNT(*) > 10 THEN 'RED'
        WHEN 100.0 * COUNT(CASE WHEN e.lifecycle_status = 'returned' THEN 1 END) / COUNT(*) > 5 THEN 'YELLOW'
        ELSE 'GREEN'
    END AS return_health
FROM {{ ref('dim_equipment') }} e
LEFT JOIN {{ ref('dim_material') }} m ON e.material_number = m.material_number
GROUP BY m.material_group
