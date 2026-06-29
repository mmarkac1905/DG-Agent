/*
    Staging: MKPF — Material Document Header
    1:1 with raw_sap.mkpf.
*/

SELECT
    {{ hash_key(['MBLNR', 'MJAHR']) }} AS hk_material_document,

    MBLNR,
    MJAHR,
    CASE WHEN BUDAT IS NOT NULL AND BUDAT != '' AND LENGTH(BUDAT) = 8
        THEN CAST(SUBSTR(BUDAT, 1, 4) || '-' || SUBSTR(BUDAT, 5, 2) || '-' || SUBSTR(BUDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BUDAT,
    CASE WHEN BLDAT IS NOT NULL AND BLDAT != '' AND LENGTH(BLDAT) = 8
        THEN CAST(SUBSTR(BLDAT, 1, 4) || '-' || SUBSTR(BLDAT, 5, 2) || '-' || SUBSTR(BLDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BLDAT,
    USNAM,
    BKTXT,
    XBLNR,

    {{ hashdiff(['BUDAT', 'BLDAT', 'USNAM', 'BKTXT', 'XBLNR']) }} AS hashdiff_mkpf,

    'SAP_MM_MKPF' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'mkpf') }}
