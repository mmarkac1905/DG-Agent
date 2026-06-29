/*
    Staging: EBAN — Purchase Requisition
    1:1 with raw_sap.eban.
*/

SELECT
    {{ hash_key(['BANFN']) }} AS hk_purchase_requisition,
    {{ hash_key(['MATNR']) }} AS hk_material,
    {{ hash_key(['WERKS']) }} AS hk_plant,

    BANFN,
    BNFPO,
    MATNR,
    WERKS,
    LGORT,
    CAST(MENGE AS DECIMAL(13, 3)) AS MENGE,
    MEINS,
    CAST(PREIS AS DECIMAL(11, 2)) AS PREIS,
    CASE WHEN BADAT IS NOT NULL AND BADAT != '' AND LENGTH(BADAT) = 8
        THEN CAST(SUBSTR(BADAT, 1, 4) || '-' || SUBSTR(BADAT, 5, 2) || '-' || SUBSTR(BADAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BADAT,
    CASE WHEN FRGDT IS NOT NULL AND FRGDT != '' AND LENGTH(FRGDT) = 8
        THEN CAST(SUBSTR(FRGDT, 1, 4) || '-' || SUBSTR(FRGDT, 5, 2) || '-' || SUBSTR(FRGDT, 7, 2) AS DATE)
        ELSE NULL
    END AS FRGDT,
    ERNAM,
    ESTKZ,
    STATU,
    EKGRP,
    EKORG,

    {{ hashdiff(['MATNR', 'WERKS', 'LGORT', 'MENGE', 'MEINS', 'PREIS', 'STATU', 'ESTKZ', 'EKGRP', 'EKORG']) }} AS hashdiff_eban,

    'SAP_MM_EBAN' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'eban') }}
