/*
    Staging: BSEG — Accounting Document Line Item
    1:1 with raw_sap.bseg.
*/

SELECT
    {{ hash_key(['BELNR', 'GJAHR', 'BUKRS']) }} AS hk_accounting_document,
    {{ hash_key(['MATNR']) }} AS hk_material,

    BUKRS,
    BELNR,
    GJAHR,
    BUZEI,
    BSCHL,
    HKONT,
    CAST(DMBTR AS DECIMAL(13, 2)) AS DMBTR,
    CAST(WRBTR AS DECIMAL(13, 2)) AS WRBTR,
    SHKZG,
    WAERS,
    MATNR,
    WERKS,

    {{ hashdiff(['BSCHL', 'HKONT', 'DMBTR', 'WRBTR', 'SHKZG', 'WAERS', 'MATNR', 'WERKS']) }} AS hashdiff_bseg,

    'SAP_FI_BSEG' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'bseg') }}
