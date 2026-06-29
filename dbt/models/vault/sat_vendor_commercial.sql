{{ config(materialized='incremental', unique_key=['hk_vendor', 'load_date']) }}

/*
    Satellite: Vendor Commercial Data (payment terms, recon account)
    Parent: hub_vendor
    Source: stg_sap__lfb1
*/

WITH src AS (
    SELECT
        hk_vendor,
        hashdiff_lfb1 AS hashdiff,
        ZTERM AS payment_terms,
        AKONT AS reconciliation_account,
        ZWELS AS payment_method,
        FDGRV AS cash_management_group,
        load_date,
        record_source
    FROM {{ ref('stg_sap__lfb1') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_vendor = src.hk_vendor
      AND t.hashdiff = src.hashdiff
)
{% endif %}
