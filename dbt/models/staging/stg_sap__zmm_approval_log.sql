/*
    Staging: ZMM_APPROVAL_LOG — Custom approval-event Z-table.
    1:1 with raw_sap.zmm_approval_log. Hash keys + hashdiff per RULE 4
    (mechanical layer, no business logic). One row per approval event.
*/

SELECT
    {{ hash_key(['APPROVAL_ID']) }} AS hk_zmm_approval,
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,
    {{ hash_key(['BELNR', 'GJAHR']) }} AS hk_invoice,

    APPROVAL_ID,
    BELNR,
    GJAHR,
    EBELN,
    EBELP,
    APPR_STATUS,
    APPR_DATE,
    APPR_USER,
    REASON_CODE,
    CAST(TOL_AMT AS DECIMAL(13, 2)) AS TOL_AMT,
    SGTXT,

    {{ hashdiff(['APPR_STATUS', 'APPR_DATE', 'APPR_USER', 'REASON_CODE', 'TOL_AMT', 'SGTXT', 'EBELN', 'EBELP', 'BELNR', 'GJAHR']) }} AS hashdiff_zmm_approval,

    'SAP_MM_ZMM_APPROVAL_LOG' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'zmm_approval_log') }}
