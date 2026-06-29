/*
    Staging: EQBS — Equipment Status History
    1:1 with raw_sap.eqbs.
*/

SELECT
    {{ hash_key(['EQUNR']) }} AS hk_equipment,

    EQUNR,
    CASE WHEN BEGDT IS NOT NULL AND BEGDT != '' AND LENGTH(BEGDT) = 8
        THEN CAST(SUBSTR(BEGDT, 1, 4) || '-' || SUBSTR(BEGDT, 5, 2) || '-' || SUBSTR(BEGDT, 7, 2) AS DATE)
        ELSE NULL
    END AS BEGDT,
    USTXT,
    STAT_DESC,

    {{ hashdiff(['USTXT', 'STAT_DESC']) }} AS hashdiff_eqbs,

    'SAP_PM_EQBS' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'eqbs') }}
