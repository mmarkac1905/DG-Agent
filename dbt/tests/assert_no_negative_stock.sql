-- Stock should never be negative
SELECT material_number, plant_code, unrestricted_stock
FROM {{ ref('fact_inventory') }}
WHERE unrestricted_stock < 0
