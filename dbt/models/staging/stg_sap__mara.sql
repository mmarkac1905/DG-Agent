/*
    Staging: MARA — Material General Data
    1:1 with raw_sap.mara.
*/

SELECT
    {{ hash_key(['MATNR']) }} AS hk_material,

    MATNR,
    MTART,
    MATKL,
    MEINS,
    CAST(BRGEW AS DECIMAL(13, 3)) AS BRGEW,
    GEWEI,
    MSTAE,
    CASE WHEN ERDAT IS NOT NULL AND ERDAT != '' AND LENGTH(ERDAT) = 8
        THEN CAST(SUBSTR(ERDAT, 1, 4) || '-' || SUBSTR(ERDAT, 5, 2) || '-' || SUBSTR(ERDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS ERDAT,
    ERNAM,
    SPART,
    PRDHA,

    {{ hashdiff(['MTART', 'MATKL', 'MEINS', 'BRGEW', 'GEWEI', 'MSTAE', 'SPART', 'PRDHA']) }} AS hashdiff_mara,

    'SAP_MM_MARA' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'mara') }}
