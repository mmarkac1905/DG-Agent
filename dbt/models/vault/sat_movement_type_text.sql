{{ config(materialized='incremental', unique_key=['hk_movement_type', 'spras', 'load_date']) }}

/*
    Satellite: Movement Type Text (multi-language)
    Parent: hub_movement_type
    Source: stg_sap__t156t

    SAP-native text table — one row per (BWART, SPRAS) where SPRAS is
    the 1-char language code (E=English, H=Croatian).

    Compound unique_key includes SPRAS because hub_movement_type is
    keyed on BWART alone but a single hub instance has multiple text
    rows (one per language). Same pattern as sat_invoice_item over
    hub_invoice — multi-row sat by design, not per-hub-instance change
    tracking.
*/

WITH src AS (
    SELECT
        hk_movement_type,
        SPRAS AS spras,
        hashdiff_t156t AS hashdiff,
        SPRAS AS language_code,
        BTEXT AS short_text,
        LTEXT AS long_text,
        load_date,
        record_source
    FROM {{ ref('stg_sap__t156t') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_movement_type = src.hk_movement_type
      AND t.spras = src.spras
      AND t.hashdiff = src.hashdiff
)
{% endif %}
