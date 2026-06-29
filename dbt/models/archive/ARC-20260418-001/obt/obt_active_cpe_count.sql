WITH active_cpe AS (
    SELECT
        equipment_number,
        serial_number,
        material_number,
        lifecycle_status,
        model_description,
        manufacturer,
        startup_date
    FROM {{ ref('dim_equipment') }}
    WHERE lifecycle_status = 'INST'
        AND serial_number IS NOT NULL
        AND equipment_category = 'CPE'
)
SELECT
    COUNT(DISTINCT serial_number) AS active_deployed_cpe_count,
    COUNT(DISTINCT equipment_number) AS active_deployed_equipment_count,
    COUNT(DISTINCT material_number) AS unique_cpe_materials,
    COUNT(DISTINCT manufacturer) AS unique_vendors,
    MIN(startup_date) AS earliest_deployment,
    MAX(startup_date) AS latest_deployment,
    CURRENT_DATE AS snapshot_date
FROM active_cpe
