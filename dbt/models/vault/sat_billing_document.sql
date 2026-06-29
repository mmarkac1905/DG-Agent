{{ config(materialized='incremental', unique_key=['hk_billing_doc', 'load_date']) }}

/*
    Satellite: Billing Document (monthly service invoice)
    Parent: hub_billing_document
    Source: stg_sap__vbrk. NETWR is the service revenue; FKDAT gives the month;
    FKSTO='X' marks a cancelled document (excluded from margin).
*/

WITH src AS (
    SELECT
        hk_billing_doc,
        hashdiff_vbrk AS hashdiff,
        FKDAT AS billing_date,
        FKART AS billing_type,
        FKSTO AS cancelled_flag,
        KUNRG AS payer_id,
        NETWR AS revenue_amount,
        MWSBK AS tax_amount,
        WAERK AS currency,
        BUKRS AS company_code,
        load_date,
        record_source
    FROM {{ ref('stg_sap__vbrk') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_billing_doc = src.hk_billing_doc
      AND t.hashdiff = src.hashdiff
)
{% endif %}
