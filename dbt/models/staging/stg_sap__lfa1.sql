/*
    Staging: LFA1 — Vendor General Data
    1:1 with raw_sap.lfa1.
*/

SELECT
    {{ hash_key(['LIFNR']) }} AS hk_vendor,

    LIFNR,
    NAME1,
    LAND1,
    ORT01,
    STRAS,
    TELF1,
    ADRNR,
    CASE WHEN ERDAT IS NOT NULL AND ERDAT != '' AND LENGTH(ERDAT) = 8
        THEN CAST(SUBSTR(ERDAT, 1, 4) || '-' || SUBSTR(ERDAT, 5, 2) || '-' || SUBSTR(ERDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS ERDAT,
    ERNAM,

    {{ hashdiff(['NAME1', 'LAND1', 'ORT01', 'STRAS', 'TELF1', 'ADRNR']) }} AS hashdiff_lfa1,

    'SAP_MM_LFA1' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'lfa1') }}
