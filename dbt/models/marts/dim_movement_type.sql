{{ config(materialized='table') }}

/*
    Dimension: Movement Type — RULE 3 compliant (vault-sourced).

    Joins hub_movement_type + sat_movement_type + sat_movement_type_text
    (filtered to EN + HR languages). All derivations are from SAP-native
    fields (BWARK, KZBEW, SHKZG) rather than a hand-curated seed.

    Refactored 2026-05-05 to drop the dependency on the
    movement_type_mapping seed. process_step is HT-domain categorization
    expressed as CASE WHEN on BWART here (no separate Z-table); SAP
    doesn't carry that classification natively but it's analyst-chosen
    metadata that belongs in the mart definition, not in data.
*/

WITH mt_cfg AS (
    SELECT *
    FROM {{ ref('sat_movement_type') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_movement_type') }})
),

mt_text AS (
    SELECT *
    FROM {{ ref('sat_movement_type_text') }}
    WHERE load_date = (SELECT MAX(load_date) FROM {{ ref('sat_movement_type_text') }})
),

text_en AS (SELECT hk_movement_type, short_text AS description_en FROM mt_text WHERE spras = 'E'),
text_hr AS (SELECT hk_movement_type, short_text AS description_hr FROM mt_text WHERE spras = 'H')

SELECT
    h.movement_type,
    en.description_en,
    hr.description_hr,

    -- Stock-flow direction. Derived from SHKZG (S=stock debit/increase,
    -- H=credit/decrease) — NOT from BWARK alone. BWARK classifies by
    -- transaction TYPE (102 GR-Reversal is BWARK='A' because it's still
    -- a receipt-category movement) but the analyst-meaningful direction
    -- is the stock effect (102 DECREASES stock, so outbound). BWARK='X'
    -- handles the special transfer case where both sides happen.
    CASE
        WHEN c.movement_category = 'X' THEN 'transfer'
        WHEN c.debit_credit_indicator = 'S' THEN 'inbound'
        WHEN c.debit_credit_indicator = 'H' THEN 'outbound'
        ELSE 'unknown'
    END AS direction,

    -- HT-domain process-step classification. Derived in the mart from
    -- the BWART code; matches the categorization the prior seed carried.
    CASE h.movement_type
        WHEN '101' THEN 'goods_receipt'
        WHEN '102' THEN 'gr_reversal'
        WHEN '122' THEN 'vendor_return'
        WHEN '161' THEN 'customer_return'
        WHEN '201' THEN 'installation'
        WHEN '202' THEN 'gi_reversal'
        WHEN '301' THEN 'stock_transfer'
        WHEN '561' THEN 'initial_load'
        ELSE 'other'
    END AS process_step,

    -- Stock impact from SHKZG (synthetic project convention:
    -- S = stock-account debit = increase, H = credit = decrease;
    -- BWARK='X' transfers are neutral at this aggregate).
    CASE
        WHEN c.movement_category = 'X' THEN 0
        WHEN c.debit_credit_indicator = 'S' THEN 1
        WHEN c.debit_credit_indicator = 'H' THEN -1
        ELSE 0
    END AS stock_impact_sign,

    CASE
        WHEN c.movement_category = 'X' THEN 'neutral'
        WHEN c.debit_credit_indicator = 'S' THEN 'increase'
        WHEN c.debit_credit_indicator = 'H' THEN 'decrease'
        ELSE 'neutral'
    END AS stock_impact_description

FROM {{ ref('hub_movement_type') }} h
JOIN mt_cfg c ON h.hk_movement_type = c.hk_movement_type
LEFT JOIN text_en en ON h.hk_movement_type = en.hk_movement_type
LEFT JOIN text_hr hr ON h.hk_movement_type = hr.hk_movement_type
