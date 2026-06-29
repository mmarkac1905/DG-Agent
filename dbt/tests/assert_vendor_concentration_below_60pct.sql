-- No vendor should exceed 60% of spend (procurement rule PR004)
SELECT vendor_name, year_quarter, ROUND(vendor_spend_share * 100, 1) AS spend_share_pct
FROM {{ ref('obt_vendor_scorecard') }}
WHERE concentration_risk_flag = TRUE
