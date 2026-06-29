/*
    Staging: EKBE — Purchasing Document History
    1:1 with raw_sap.ekbe.
*/

SELECT
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,
    {{ hash_key(['BELNR', 'GJAHR']) }} AS hk_material_document,

    EBELN,
    EBELP,
    ZEKKN,
    VGABE,
    GJAHR,
    BELNR,
    BUZEI,
    CASE WHEN BUDAT IS NOT NULL AND BUDAT != '' AND LENGTH(BUDAT) = 8
        THEN CAST(SUBSTR(BUDAT, 1, 4) || '-' || SUBSTR(BUDAT, 5, 2) || '-' || SUBSTR(BUDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BUDAT,
    CAST(MENGE AS DECIMAL(13, 3)) AS MENGE,
    CAST(DMBTR AS DECIMAL(13, 2)) AS DMBTR,
    WAERS,
    BWART,

    {{ hashdiff(['VGABE', 'BUDAT', 'MENGE', 'DMBTR', 'WAERS', 'BWART']) }} AS hashdiff_ekbe,

    'SAP_MM_EKBE' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'ekbe') }}
