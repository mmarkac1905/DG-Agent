{{ config(materialized='incremental', unique_key=['hk_movement_type', 'load_date']) }}

/*
    Satellite: Movement Type Configuration
    Parent: hub_movement_type
    Source: stg_sap__t156

    Carries SAP-native config fields per real T156:
    - BWARK (movement category) — A=receipt, B=issue, X=stock transfer
    - KZBEW (movement indicator) — B=GR-for-PO, F=consumption, L=stock-transport, etc.
    - SHKZG (debit/credit) — S=stock debit (synthetic: increase), H=credit (synthetic: decrease)

    Multi-language descriptions are in sat_movement_type_text (sourced
    from T156T) — keep them out of here per real SAP separation of
    concerns.
*/

WITH src AS (
    SELECT
        hk_movement_type,
        hashdiff_t156 AS hashdiff,
        BWARK AS movement_category,
        KZBEW AS movement_indicator,
        SHKZG AS debit_credit_indicator,
        load_date,
        record_source
    FROM {{ ref('stg_sap__t156') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_movement_type = src.hk_movement_type
      AND t.hashdiff = src.hashdiff
)
{% endif %}
