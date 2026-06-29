{{ config(materialized='incremental', unique_key=['hk_zmm_approval', 'load_date']) }}

/*
    Satellite: ZMM Approval Attributes
    Parent: hub_zmm_approval
    Source: stg_sap__zmm_approval_log

    Carries the approval-event attributes plus the natural keys that
    link the event back to a PO line (EBELN/EBELP) and an invoice
    (BELNR/GJAHR). Marts can join on natural keys without a dedicated
    link table for this Z-event use case.
*/

WITH src AS (
    SELECT
        hk_zmm_approval,
        hashdiff_zmm_approval AS hashdiff,
        APPR_STATUS AS approval_status,
        APPR_DATE AS approval_date,
        APPR_USER AS approval_user,
        REASON_CODE AS reason_code,
        TOL_AMT AS tolerance_amount,
        SGTXT AS approval_text,
        EBELN AS purchase_order_number,
        EBELP AS po_item_number,
        BELNR AS invoice_number,
        GJAHR AS fiscal_year,
        load_date,
        record_source
    FROM {{ ref('stg_sap__zmm_approval_log') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE NOT EXISTS (
    SELECT 1 FROM {{ this }} t
    WHERE t.hk_zmm_approval = src.hk_zmm_approval
      AND t.hashdiff = src.hashdiff
)
{% endif %}
