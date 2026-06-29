/*
    Staging: EQUI — Equipment Master
    1:1 with raw_sap.equi.
*/

SELECT
    {{ hash_key(['EQUNR']) }} AS hk_equipment,
    {{ hash_key(['MATNR']) }} AS hk_material,

    EQUNR,
    MATNR,
    SERGE,
    HERST,
    TYPBZ,
    CASE WHEN INBDT IS NOT NULL AND INBDT != '' AND LENGTH(INBDT) = 8
        THEN CAST(SUBSTR(INBDT, 1, 4) || '-' || SUBSTR(INBDT, 5, 2) || '-' || SUBSTR(INBDT, 7, 2) AS DATE)
        ELSE NULL
    END AS INBDT,
    CASE WHEN ERDAT IS NOT NULL AND ERDAT != '' AND LENGTH(ERDAT) = 8
        THEN CAST(SUBSTR(ERDAT, 1, 4) || '-' || SUBSTR(ERDAT, 5, 2) || '-' || SUBSTR(ERDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS ERDAT,
    ERNAM,
    GEWRK,
    EQART,
    STAT_TEXT,

    {{ hashdiff(['SERGE', 'HERST', 'TYPBZ', 'INBDT', 'GEWRK', 'EQART', 'STAT_TEXT']) }} AS hashdiff_equi,

    'SAP_PM_EQUI' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'equi') }}
