{{ config(materialized='table') }}

/*
    Fact: Goods Movements — grain = material document item.
    Joins link_gr_material + sat_gr_header + sat_gr_item.
*/

SELECT
    gri.hk_gr_material,
    hmd.hk_material_document,
    lgm.hk_material,

    hmd.material_document_number,
    hmd.fiscal_year,
    gri.document_item,
    lgm.material_number,

    grh.posting_date,
    grh.document_date,
    grh.posted_by,
    grh.header_text,
    grh.reference_document,

    gri.movement_type,
    gri.plant,
    gri.storage_location,
    gri.quantity,
    gri.unit_of_measure,
    gri.amount_local_currency,
    gri.currency,
    gri.purchase_order_number,
    gri.po_item_number,
    gri.vendor_id,

    CASE
        WHEN gri.movement_type = '101' THEN 'goods_receipt'
        WHEN gri.movement_type = '102' THEN 'gr_reversal'
        WHEN gri.movement_type = '122' THEN 'vendor_return'
        WHEN gri.movement_type = '161' THEN 'customer_return'
        WHEN gri.movement_type = '201' THEN 'deployment'
        WHEN gri.movement_type = '202' THEN 'gi_reversal'
        WHEN gri.movement_type = '301' THEN 'plant_transfer'
        WHEN gri.movement_type = '561' THEN 'initial_stock'
        ELSE 'other'
    END AS movement_category,

    CASE
        WHEN gri.movement_type IN ('101', '161', '202', '561') THEN 'inbound'
        WHEN gri.movement_type IN ('102', '122', '201') THEN 'outbound'
        WHEN gri.movement_type = '301' THEN 'transfer'
        ELSE 'unknown'
    END AS movement_direction,

    CASE
        WHEN gri.movement_type IN ('101', '161', '202', '561') THEN gri.quantity
        WHEN gri.movement_type IN ('102', '122', '201') THEN -1 * gri.quantity
        ELSE 0
    END AS signed_quantity,

    CASE WHEN gri.purchase_order_number IS NOT NULL AND gri.purchase_order_number != ''
        THEN TRUE ELSE FALSE
    END AS has_po_reference,

    grh.record_source

FROM {{ ref('link_gr_material') }} lgm
JOIN {{ ref('hub_material_document') }} hmd ON lgm.hk_material_document = hmd.hk_material_document
LEFT JOIN {{ ref('sat_gr_header') }} grh ON hmd.hk_material_document = grh.hk_material_document
LEFT JOIN {{ ref('sat_gr_item') }} gri ON lgm.hk_gr_material = gri.hk_gr_material
