{{ config(materialized='table') }}

/*
    Fact: Purchase Orders — grain = PO item.
    Business metrics computed:
    - lead_time_days (BG001) — PO date to first GR
    - is_on_time (BG002) — GR <= scheduled + 2 day tolerance
    - po_cycle_days (BG008) — PR date to PO date
*/

WITH po_header AS (
    SELECT * FROM {{ ref('sat_po_header') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_po_header') }})
),

po_items AS (
    SELECT * FROM {{ ref('sat_po_item') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_po_item') }})
),

po_schedule AS (
    SELECT * FROM {{ ref('sat_po_schedule') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_po_schedule') }})
),

po_account AS (
    SELECT * FROM {{ ref('sat_po_account') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_po_account') }})
),

-- First GR date per PO (lead time anchor)
first_gr AS (
    SELECT
        lgp.hk_purchase_order,
        lgp.purchase_order_number,
        MIN(grh.posting_date) AS first_gr_date
    FROM {{ ref('link_gr_po') }} lgp
    JOIN {{ ref('sat_gr_header') }} grh
        ON lgp.hk_material_document = grh.hk_material_document
    GROUP BY lgp.hk_purchase_order, lgp.purchase_order_number
),

-- Aggregate GR quantity per PO item.
-- sat_gr_item preserves EBELN/EBELP from source, so no hash-key join needed.
gr_totals AS (
    SELECT
        purchase_order_number,
        po_item_number,
        SUM(CASE WHEN movement_type = '101' THEN quantity ELSE 0 END) AS total_gr_quantity,
        SUM(CASE WHEN movement_type = '102' THEN quantity ELSE 0 END) AS total_gr_reversal_quantity
    FROM {{ ref('sat_gr_item') }}
    WHERE movement_type IN ('101', '102')
      AND purchase_order_number IS NOT NULL
      AND purchase_order_number != ''
    GROUP BY purchase_order_number, po_item_number
),

pr_dates AS (
    SELECT
        lpp.hk_purchase_order,
        lpp.purchase_order_number,
        prd.requisition_date
    FROM {{ ref('link_pr_po') }} lpp
    JOIN {{ ref('sat_pr_detail') }} prd
        ON lpp.hk_purchase_requisition = prd.hk_purchase_requisition
),

po_vendor AS (
    SELECT hk_purchase_order, hk_vendor, vendor_id FROM {{ ref('link_po_vendor') }}
),

-- Build po_material with both hk_po_material AND hk_po_item so we can join sat_po_schedule/sat_po_account
po_material AS (
    SELECT
        hk_po_material,
        {{ hash_key(['purchase_order_number', 'po_item_number']) }} AS hk_po_item,
        hk_purchase_order,
        hk_material,
        purchase_order_number,
        po_item_number,
        material_number
    FROM {{ ref('link_po_material') }}
)

SELECT
    pm.hk_po_material,
    pm.hk_purchase_order,
    pm.hk_material,
    pv.hk_vendor,

    pm.purchase_order_number,
    pm.po_item_number,
    pm.material_number,
    pv.vendor_id,

    ph.po_date,
    ph.document_type,
    ph.purchasing_organization,
    ph.purchasing_group,
    ph.currency,
    ph.total_po_value,
    ph.processing_status,
    ph.created_by,

    pi.item_short_text,
    pi.ordered_quantity,
    pi.unit_of_measure,
    pi.unit_price,
    pi.net_value,
    pi.material_group,
    pi.item_category,
    pi.plant,
    pi.storage_location,

    ps.scheduled_delivery_date,
    ps.scheduled_quantity,
    ps.goods_received_quantity AS schedule_gr_quantity,

    pa.gl_account,
    pa.cost_center,

    fgr.first_gr_date,
    prd.requisition_date AS pr_date,

    -- BG001: lead time (PO -> first GR)
    CASE WHEN fgr.first_gr_date IS NOT NULL AND ph.po_date IS NOT NULL
        THEN CAST(fgr.first_gr_date - ph.po_date AS INTEGER)
        ELSE NULL
    END AS lead_time_days,

    -- BG002: on-time (GR <= scheduled + 2 day tolerance)
    CASE
        WHEN fgr.first_gr_date IS NOT NULL AND ps.scheduled_delivery_date IS NOT NULL
        THEN CASE WHEN fgr.first_gr_date <= ps.scheduled_delivery_date + INTERVAL 2 DAY
                  THEN TRUE ELSE FALSE END
        ELSE NULL
    END AS is_on_time,

    -- BG008: PO cycle time (PR -> PO)
    CASE WHEN prd.requisition_date IS NOT NULL AND ph.po_date IS NOT NULL
        THEN CAST(ph.po_date - prd.requisition_date AS INTEGER)
        ELSE NULL
    END AS po_cycle_days,

    CASE
        WHEN fgr.first_gr_date IS NULL THEN 'pending'
        WHEN gr_totals.total_gr_quantity >= pi.ordered_quantity THEN 'fully_received'
        WHEN gr_totals.total_gr_quantity > 0 THEN 'partially_received'
        ELSE 'pending'
    END AS delivery_status,

    COALESCE(gr_totals.total_gr_quantity, 0) AS received_quantity,
    COALESCE(gr_totals.total_gr_reversal_quantity, 0) AS reversed_quantity,
    COALESCE(gr_totals.total_gr_quantity, 0) * pi.unit_price AS received_value,

    ph.record_source

FROM po_material pm
LEFT JOIN po_header ph ON pm.hk_purchase_order = ph.hk_purchase_order
LEFT JOIN po_items pi ON pm.hk_po_material = pi.hk_po_material
LEFT JOIN po_schedule ps ON pm.hk_po_item = ps.hk_po_item
LEFT JOIN po_account pa ON pm.hk_po_item = pa.hk_po_item
LEFT JOIN po_vendor pv ON pm.hk_purchase_order = pv.hk_purchase_order
LEFT JOIN first_gr fgr ON pm.hk_purchase_order = fgr.hk_purchase_order
LEFT JOIN gr_totals
    ON pm.purchase_order_number = gr_totals.purchase_order_number
    AND pm.po_item_number = gr_totals.po_item_number
LEFT JOIN pr_dates prd ON pm.hk_purchase_order = prd.hk_purchase_order
