{{ config(materialized='view') }}

/*
    OBT: Procurement Overview — fact_purchase_orders joined with all dims.
*/

SELECT
    f.purchase_order_number,
    f.po_item_number,
    f.po_date,
    d.year AS po_year,
    d.quarter AS po_quarter,
    d.month AS po_month,
    d.year_quarter AS po_year_quarter,
    d.year_month AS po_year_month,
    v.vendor_id,
    v.vendor_name,
    v.country_code AS vendor_country,
    v.city AS vendor_city,
    v.vendor_region,
    v.payment_terms,
    f.material_number,
    m.material_description,
    m.material_group,
    m.equipment_category,
    f.plant,
    f.storage_location,
    f.cost_center,
    f.ordered_quantity,
    f.unit_price,
    f.net_value,
    f.currency,
    f.received_quantity,
    f.received_value,
    f.scheduled_delivery_date,
    f.first_gr_date,
    f.delivery_status,
    f.lead_time_days,
    f.is_on_time,
    f.po_cycle_days,
    f.pr_date,
    f.processing_status,
    f.document_type,
    f.purchasing_organization,
    f.purchasing_group,
    f.created_by
FROM {{ ref('fact_purchase_orders') }} f
LEFT JOIN {{ ref('dim_vendor') }} v ON f.hk_vendor = v.hk_vendor
LEFT JOIN {{ ref('dim_material') }} m ON f.hk_material = m.hk_material
LEFT JOIN {{ ref('dim_date') }} d ON f.po_date = d.date_key
