{{ config(materialized='incremental', unique_key='hk_so_equipment') }}

/*
    Link: Sales Order Item ↔ Equipment (the marquee tie)
    Resolves VBAP.SERNR to the physical CPE device (equi.SERGE), so service
    revenue can be attributed to a specific router model (equi.MATNR). This is
    the join deliberately deferred out of staging per RULE 4.
    Carries hk_material (the CPE router) — the bridge from revenue to device cost.
    Source: stg_sap__vbap JOIN stg_sap__equi ON SERNR = SERGE
*/

WITH src AS (
    SELECT DISTINCT
        {{ hash_key(['p.VBELN', 'p.POSNR', 'p.SERNR']) }} AS hk_so_equipment,
        p.hk_sales_order,
        p.hk_sales_order_item,
        e.hk_equipment,
        e.hk_material,
        p.VBELN AS sales_order_number,
        p.POSNR AS sales_order_item,
        p.SERNR AS device_serial,
        e.EQUNR AS equipment_number,
        e.MATNR AS cpe_material_number,
        p.load_date,
        p.record_source
    FROM {{ ref('stg_sap__vbap') }} p
    JOIN {{ ref('stg_sap__equi') }} e ON p.SERNR = e.SERGE
)

SELECT * FROM src
{{ vault_source_filter('src', 'hk_so_equipment') }}
