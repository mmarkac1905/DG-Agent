/*
    Staging: EKKO — Purchasing Document Header
    1:1 with raw_sap.ekko. No joins, no renaming.
    Adds: hash keys, hashdiff, record_source, load_date, date casting.
*/

SELECT
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,
    {{ hash_key(['LIFNR']) }} AS hk_vendor,

    EBELN,
    BUKRS,
    BSTYP,
    BSART,
    LIFNR,
    EKORG,
    EKGRP,
    CASE WHEN BEDAT IS NOT NULL AND BEDAT != '' AND LENGTH(BEDAT) = 8
        THEN CAST(SUBSTR(BEDAT, 1, 4) || '-' || SUBSTR(BEDAT, 5, 2) || '-' || SUBSTR(BEDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BEDAT,
    CASE WHEN KDATB IS NOT NULL AND KDATB != '' AND LENGTH(KDATB) = 8
        THEN CAST(SUBSTR(KDATB, 1, 4) || '-' || SUBSTR(KDATB, 5, 2) || '-' || SUBSTR(KDATB, 7, 2) AS DATE)
        ELSE NULL
    END AS KDATB,
    CASE WHEN KDATE IS NOT NULL AND KDATE != '' AND LENGTH(KDATE) = 8
        THEN CAST(SUBSTR(KDATE, 1, 4) || '-' || SUBSTR(KDATE, 5, 2) || '-' || SUBSTR(KDATE, 7, 2) AS DATE)
        ELSE NULL
    END AS KDATE,
    WAERS,
    CAST(WKURS AS DECIMAL(9, 5)) AS WKURS,
    ERNAM,
    CASE WHEN AEDAT IS NOT NULL AND AEDAT != '' AND LENGTH(AEDAT) = 8
        THEN CAST(SUBSTR(AEDAT, 1, 4) || '-' || SUBSTR(AEDAT, 5, 2) || '-' || SUBSTR(AEDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS AEDAT,
    PROCSTAT,
    CAST(RLWRT AS DECIMAL(15, 2)) AS RLWRT,
    BANFN,

    {{ hashdiff(['BUKRS', 'BSTYP', 'BSART', 'LIFNR', 'EKORG', 'EKGRP', 'BEDAT', 'WAERS', 'WKURS', 'PROCSTAT', 'RLWRT', 'BANFN']) }} AS hashdiff_ekko,

    'SAP_MM_EKKO' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'ekko') }}
