-- Defect rate should not exceed 5% for any CPE category (procurement rule PR005)
SELECT equipment_category, defect_rate_pct
FROM {{ ref('knowledge_cpe_lifecycle_metrics') }}
WHERE defect_rate_pct > 5.0
