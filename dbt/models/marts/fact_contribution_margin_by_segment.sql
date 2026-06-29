{{ config(
    materialized='table',
    tags=['finance', 'contribution_margin', 'cpe']
) }}

-- RULE 3 compliant: refs VAULT models only (hubs/links/sats), never staging.
-- CPE contribution margin = billed service revenue - amortized device cost,
-- at grain service_plan x tenure_band x billing_month.

WITH

-- Revenue: non-cancelled billing docs, linked to their sales order
billing AS (
    SELECT
        sbd.hk_billing_doc,
        DATE_TRUNC('month', CAST(sbd.billing_date AS DATE))   AS billing_month,
        sbd.payer_id                                          AS customer_id,
        CAST(sbd.revenue_amount AS NUMERIC)                   AS line_revenue_eur,
        lbso.hk_sales_order
    FROM {{ ref('sat_billing_document') }} AS sbd
    INNER JOIN {{ ref('link_billing_sales_order') }} AS lbso
        ON lbso.hk_billing_doc = sbd.hk_billing_doc
    WHERE COALESCE(sbd.cancelled_flag, '') != 'X'
),

-- Tenure anchor: each customer's first non-cancelled billing date
tenure_anchor AS (
    SELECT
        sbd.payer_id                                          AS customer_id,
        MIN(CAST(sbd.billing_date AS DATE))                   AS first_billing_date
    FROM {{ ref('sat_billing_document') }} AS sbd
    WHERE COALESCE(sbd.cancelled_flag, '') != 'X'
      AND sbd.billing_date IS NOT NULL
    GROUP BY sbd.payer_id
),

-- Sales order -> deployed device + service plan
so_device AS (
    SELECT
        lse.hk_sales_order,
        lse.hk_equipment,
        CASE ssi.service_plan_group
            WHEN 'SVC-FIB' THEN 'fiber'
            WHEN 'SVC-TV'  THEN 'TV'
            WHEN 'SVC-CBL' THEN 'cable'
            WHEN 'SVC-BIZ' THEN 'business'
            ELSE 'unknown'
        END                                                   AS service_plan
    FROM {{ ref('link_sales_order_equipment') }} AS lse
    INNER JOIN {{ ref('sat_sales_order_item') }} AS ssi
        ON ssi.hk_so_equipment = lse.hk_so_equipment
),

-- Deployment date = equipment startup_date (INBDT); first-bill fallback applied at use site (decision #119)
device_deploy AS (
    SELECT
        seg.hk_equipment,
        CAST(seg.startup_date AS DATE)                        AS deployment_date_raw
    FROM {{ ref('sat_equipment_general') }} AS seg
    -- SCD2 satellite: keep only the current version per equipment (avoids fanout)
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY seg.hk_equipment ORDER BY seg.load_date DESC
    ) = 1
),

-- Device unit cost: equipment -> GR -> PO -> PO-material -> unit_price; averaged per device, /24 for monthly amortization
device_cost AS (
    SELECT
        lse.hk_equipment,
        AVG(CAST(spi.unit_price AS NUMERIC)) / 24.0           AS monthly_amort_cost_eur
    FROM {{ ref('link_sales_order_equipment') }} AS lse
    INNER JOIN {{ ref('link_equipment_gr') }} AS leg
        ON leg.hk_equipment = lse.hk_equipment
    INNER JOIN {{ ref('link_gr_po') }} AS lgp
        ON lgp.hk_material_document = leg.hk_material_document
    INNER JOIN {{ ref('link_po_material') }} AS lpm
        ON lpm.hk_purchase_order = lgp.hk_purchase_order
       AND lpm.hk_material       = lse.hk_material
    INNER JOIN {{ ref('sat_po_item') }} AS spi
        ON spi.hk_po_material = lpm.hk_po_material
    WHERE spi.unit_price > 0
    GROUP BY lse.hk_equipment
),

-- Enrich billing with customer tenure, device deployment, and device cost
billing_enriched AS (
    SELECT
        b.billing_month,
        b.hk_billing_doc,
        b.customer_id,
        sd.service_plan,
        b.line_revenue_eur,
        DATEDIFF('month', ta.first_billing_date, b.billing_month) AS tenure_months,
        -- deployment_date = COALESCE(INBDT, first_bill) per decision #119
        COALESCE(dd.deployment_date_raw, ta.first_billing_date)  AS deployment_date,
        dc.monthly_amort_cost_eur
    FROM billing b
    LEFT JOIN tenure_anchor ta ON ta.customer_id    = b.customer_id
    LEFT JOIN so_device     sd ON sd.hk_sales_order = b.hk_sales_order
    LEFT JOIN device_deploy dd ON dd.hk_equipment   = sd.hk_equipment
    LEFT JOIN device_cost   dc ON dc.hk_equipment   = sd.hk_equipment
),

-- Charge amortized cost only while billing month is inside the 24-month window; assign tenure band
billing_costed AS (
    SELECT
        billing_month,
        hk_billing_doc,
        customer_id,
        COALESCE(service_plan, 'unknown')                     AS service_plan,
        line_revenue_eur,
        CASE
            WHEN deployment_date IS NOT NULL
             AND billing_month >= deployment_date
             AND billing_month <  (deployment_date + INTERVAL '24 months')
            THEN COALESCE(monthly_amort_cost_eur, 0)
            ELSE 0
        END                                                   AS charged_amort_cost_eur,
        CASE
            WHEN tenure_months IS NULL THEN 'unknown'
            WHEN tenure_months <  12   THEN '0-12'
            WHEN tenure_months <  24   THEN '12-24'
            WHEN tenure_months <  48   THEN '24-48'
            ELSE '48+'
        END                                                   AS tenure_band
    FROM billing_enriched
)

SELECT
    billing_month,
    service_plan,
    tenure_band,
    SUM(line_revenue_eur)                                     AS total_billed_revenue_eur,
    SUM(charged_amort_cost_eur)                               AS total_amortized_device_cost_eur,
    SUM(line_revenue_eur) - SUM(charged_amort_cost_eur)       AS contribution_margin_eur,
    COUNT(DISTINCT customer_id)                               AS distinct_customers,
    COUNT(DISTINCT hk_billing_doc)                            AS billing_doc_count
FROM billing_costed
GROUP BY billing_month, service_plan, tenure_band
ORDER BY billing_month, service_plan, tenure_band
