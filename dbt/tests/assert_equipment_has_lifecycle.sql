-- Every equipment should have at least one lifecycle event
SELECT e.equipment_number
FROM {{ ref('dim_equipment') }} e
WHERE NOT EXISTS (
    SELECT 1 FROM {{ ref('fact_equipment_lifecycle') }} f
    WHERE f.hk_equipment = e.hk_equipment
)
