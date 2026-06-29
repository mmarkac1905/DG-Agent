/*
    Staging: RESB — Reservation Item
    1:1 with raw_sap.resb.
*/

SELECT
    {{ hash_key(['RSNUM']) }} AS hk_reservation,
    {{ hash_key(['MATNR']) }} AS hk_material,
    {{ hash_key(['WERKS']) }} AS hk_plant,

    RSNUM,
    RSPOS,
    MATNR,
    WERKS,
    LGORT,
    CAST(BDMNG AS DECIMAL(13, 3)) AS BDMNG,
    MEINS,
    CASE WHEN BDTER IS NOT NULL AND BDTER != '' AND LENGTH(BDTER) = 8
        THEN CAST(SUBSTR(BDTER, 1, 4) || '-' || SUBSTR(BDTER, 5, 2) || '-' || SUBSTR(BDTER, 7, 2) AS DATE)
        ELSE NULL
    END AS BDTER,

    {{ hashdiff(['MATNR', 'WERKS', 'LGORT', 'BDMNG', 'MEINS', 'BDTER']) }} AS hashdiff_resb,

    'SAP_MM_RESB' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'resb') }}
