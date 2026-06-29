-- Session Context for CPE Procurement Analytics
-- Run at start of every Claude Code session. Queries knowledge models for live state.

SELECT '=== PIPELINE SUMMARY ===' AS section;
SELECT layer, table_count
FROM main_knowledge.knowledge_pipeline_summary
ORDER BY
    CASE layer
        WHEN 'raw_sap' THEN 1 WHEN 'seeds' THEN 2 WHEN 'staging' THEN 3
        WHEN 'vault' THEN 4 WHEN 'marts' THEN 5 WHEN 'obt' THEN 6 WHEN 'knowledge' THEN 7
    END;

SELECT '=== PROCUREMENT HEALTH ===' AS section;
SELECT
    total_pos,
    avg_lead_time_days,
    otd_rate_pct || '% (' || otd_health || ')' AS otd_status,
    avg_po_cycle_days || 'd (' || cycle_time_health || ')' AS cycle_time_status,
    '€' || CAST(ROUND(total_spend_eur, 0) AS VARCHAR) AS total_spend,
    pending_deliveries || ' pending, ' || partial_deliveries || ' partial' AS delivery_status,
    highest_concentration_vendor || ' at ' || highest_concentration_pct || '% (' || concentration_health || ')' AS concentration_status,
    total_stock_units || ' units, ' || zero_stock_locations || ' zero-stock (' || inventory_health || ')' AS inventory_status
FROM main_knowledge.knowledge_procurement_health;

SELECT '=== VENDOR PERFORMANCE ===' AS section;
SELECT
    vendor_name,
    performance_grade,
    health_status,
    total_pos || ' POs' AS volume,
    avg_lead_time_days || 'd' AS lead_time,
    otd_rate_pct || '%' AS otd,
    '€' || CAST(ROUND(total_spend_eur, 0) AS VARCHAR) AS spend
FROM main_knowledge.knowledge_vendor_performance
ORDER BY total_spend_eur DESC;

SELECT '=== CPE LIFECYCLE ===' AS section;
SELECT
    equipment_category,
    total_devices,
    deployed || ' deployed (' || deployment_rate_pct || '%)' AS deployment,
    defective || ' defective (' || defect_rate_pct || '% - ' || defect_health || ')' AS defect_status,
    returned || ' returned (' || return_rate_pct || '% - ' || return_health || ')' AS return_status
FROM main_knowledge.knowledge_cpe_lifecycle_metrics
ORDER BY total_devices DESC;

SELECT '=== DATA QUALITY ===' AS section;
SELECT 'DQ checks enforced via dbt test (8 custom tests). Run: dbt test' AS note;

SELECT '=== INVENTORY ALERTS ===' AS section;
SELECT
    material_number,
    equipment_category,
    plant_code,
    alert_type,
    health_status,
    alert_message
FROM main_knowledge.knowledge_inventory_alerts
ORDER BY
    CASE health_status WHEN 'RED' THEN 1 WHEN 'YELLOW' THEN 2 ELSE 3 END;

SELECT '=== SESSION_AGENDA ===' AS section;

SELECT 'NEXT_SESSION' AS agenda_type, id, title, priority
FROM main_seeds.known_issues
WHERE status = 'open' AND title LIKE 'NEXT_SESSION:%'
ORDER BY priority;

SELECT 'DUE_REMINDER' AS agenda_type, id, title, priority
FROM main_seeds.known_issues
WHERE status = 'open' AND title LIKE '%REMINDER%'
ORDER BY priority;

SELECT 'OPEN_ISSUE' AS agenda_type, id, title, priority
FROM main_seeds.known_issues
WHERE status = 'open'
ORDER BY
    CASE priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 WHEN 'low' THEN 4 ELSE 5 END;

SELECT '=== NEVER REPEAT ===' AS section;
SELECT id, decision, conclusion
FROM main_seeds.known_decisions
WHERE never_repeat = 'true';
