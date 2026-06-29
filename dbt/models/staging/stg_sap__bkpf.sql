/*
    Staging: BKPF — Accounting Document Header
    1:1 with raw_sap.bkpf.
*/

SELECT
    {{ hash_key(['BELNR', 'GJAHR', 'BUKRS']) }} AS hk_accounting_document,

    BUKRS,
    BELNR,
    GJAHR,
    BLART,
    CASE WHEN BUDAT IS NOT NULL AND BUDAT != '' AND LENGTH(BUDAT) = 8
        THEN CAST(SUBSTR(BUDAT, 1, 4) || '-' || SUBSTR(BUDAT, 5, 2) || '-' || SUBSTR(BUDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BUDAT,
    CASE WHEN BLDAT IS NOT NULL AND BLDAT != '' AND LENGTH(BLDAT) = 8
        THEN CAST(SUBSTR(BLDAT, 1, 4) || '-' || SUBSTR(BLDAT, 5, 2) || '-' || SUBSTR(BLDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BLDAT,
    USNAM,
    XBLNR,
    BKTXT,
    AWTYP,
    AWKEY,

    {{ hashdiff(['BLART', 'BUDAT', 'BLDAT', 'USNAM', 'XBLNR', 'BKTXT', 'AWTYP', 'AWKEY']) }} AS hashdiff_bkpf,

    'SAP_FI_BKPF' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'bkpf') }}
