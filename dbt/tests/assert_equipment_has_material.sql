-- Every equipment record should link to a material
SELECT equipment_number
FROM {{ ref('dim_equipment') }}
WHERE material_number IS NULL
