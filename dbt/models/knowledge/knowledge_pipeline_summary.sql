{{ config(materialized='view') }}

/*
    Knowledge: Pipeline Summary — object counts per layer.
*/

WITH layer_counts AS (
    SELECT 'raw_sap' AS layer, COUNT(*) AS table_count
    FROM information_schema.tables WHERE table_schema = 'raw_sap'
    UNION ALL
    SELECT 'staging', COUNT(*)
    FROM information_schema.tables WHERE table_schema = 'main_staging'
    UNION ALL
    SELECT 'vault', COUNT(*)
    FROM information_schema.tables WHERE table_schema = 'main_vault'
    UNION ALL
    SELECT 'marts', COUNT(*)
    FROM information_schema.tables WHERE table_schema = 'main_marts'
    UNION ALL
    SELECT 'obt', COUNT(*)
    FROM information_schema.tables WHERE table_schema = 'main_obt'
    UNION ALL
    SELECT 'knowledge', COUNT(*)
    FROM information_schema.tables WHERE table_schema = 'main_knowledge'
    UNION ALL
    SELECT 'seeds', COUNT(*)
    FROM information_schema.tables WHERE table_schema = 'main_seeds'
)

SELECT
    layer,
    table_count,
    SUM(table_count) OVER () AS total_objects
FROM layer_counts
ORDER BY
    CASE layer
        WHEN 'raw_sap' THEN 1
        WHEN 'seeds' THEN 2
        WHEN 'staging' THEN 3
        WHEN 'vault' THEN 4
        WHEN 'marts' THEN 5
        WHEN 'obt' THEN 6
        WHEN 'knowledge' THEN 7
    END
