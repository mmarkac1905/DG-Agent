/*
    Staging: KNA1 — Customer General Data (SD)
    1:1 with raw_sap.kna1.
*/

SELECT
    {{ hash_key(['KUNNR']) }} AS hk_customer,

    KUNNR,
    NAME1,
    LAND1,
    ORT01,
    STRAS,
    PSTLZ,
    KTOKD,
    CASE WHEN ERDAT IS NOT NULL AND ERDAT != '' AND LENGTH(ERDAT) = 8
        THEN CAST(SUBSTR(ERDAT, 1, 4) || '-' || SUBSTR(ERDAT, 5, 2) || '-' || SUBSTR(ERDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS ERDAT,
    SPRAS,

    {{ hashdiff(['NAME1', 'LAND1', 'ORT01', 'STRAS', 'PSTLZ', 'KTOKD']) }} AS hashdiff_kna1,

    'SAP_SD_KNA1' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'kna1') }}
