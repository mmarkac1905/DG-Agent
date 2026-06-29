{{ config(
    materialized='view',
    tags=['finance', 'contribution_margin', 'cpe', 'dashboard']
) }}

SELECT
    -- Time dimension
    cm.billing_month,
    EXTRACT(YEAR  FROM cm.billing_month)            AS billing_year,
    EXTRACT(MONTH FROM cm.billing_month)            AS billing_month_num,

    -- Segment dimensions
    cm.service_plan,
    cm.tenure_band,

    -- Revenue and cost measures
    cm.total_billed_revenue_eur,
    cm.total_amortized_device_cost_eur,
    cm.contribution_margin_eur,

    -- Derived flags
    CASE
        WHEN cm.contribution_margin_eur >= 0 THEN TRUE
        ELSE FALSE
    END                                             AS is_margin_positive,

    -- Volume
    cm.distinct_customers,
    cm.billing_doc_count,

    -- Per-customer average margin
    CASE
        WHEN cm.distinct_customers > 0
        THEN cm.contribution_margin_eur / cm.distinct_customers
        ELSE NULL
    END                                             AS avg_margin_per_customer_eur,

    -- Margin rate as % of revenue
    CASE
        WHEN cm.total_billed_revenue_eur > 0
        THEN ROUND(
            100.0 * cm.contribution_margin_eur / cm.total_billed_revenue_eur,
            2
        )
        ELSE NULL
    END                                             AS contribution_margin_pct

FROM {{ ref('fact_contribution_margin_by_segment') }} AS cm
ORDER BY
    cm.billing_month,
    cm.service_plan,
    cm.tenure_band