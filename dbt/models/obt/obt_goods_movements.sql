{{ config(materialized='view') }}

/*
    OBT: Goods Movements — all material movements with full context.
*/

SELECT
    f.material_document_number,
    f.fiscal_year,
    f.document_item,
    f.posting_date,
    d.year AS posting_year,
    d.quarter AS posting_quarter,
    d.month AS posting_month,
    d.year_month AS posting_year_month,
    f.movement_type,
    mt.description_en AS movement_description,
    f.movement_category,
    f.movement_direction,
    f.quantity,
    f.signed_quantity,
    f.amount_local_currency,
    f.material_number,
    m.material_description,
    m.equipment_category,
    f.plant,
    p.plant_name,
    f.storage_location,
    f.purchase_order_number,
    f.vendor_id,
    f.has_po_reference,
    f.posted_by
FROM {{ ref('fact_goods_movements') }} f
LEFT JOIN {{ ref('dim_material') }} m ON f.hk_material = m.hk_material
LEFT JOIN {{ ref('dim_date') }} d ON f.posting_date = d.date_key
LEFT JOIN {{ ref('dim_movement_type') }} mt ON f.movement_type = mt.movement_type
LEFT JOIN {{ ref('dim_plant') }} p ON f.plant = p.plant_code
