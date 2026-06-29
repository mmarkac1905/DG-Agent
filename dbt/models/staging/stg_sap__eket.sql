/*
    Staging: EKET — Purchasing Document Delivery Schedule
    1:1 with raw_sap.eket.
*/

SELECT
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,
    {{ hash_key(['EBELN', 'EBELP']) }} AS hk_po_item,

    EBELN,
    EBELP,
    ETENR,
    CASE WHEN EINDT IS NOT NULL AND EINDT != '' AND LENGTH(EINDT) = 8
        THEN CAST(SUBSTR(EINDT, 1, 4) || '-' || SUBSTR(EINDT, 5, 2) || '-' || SUBSTR(EINDT, 7, 2) AS DATE)
        ELSE NULL
    END AS EINDT,
    CAST(MENGE AS DECIMAL(13, 3)) AS MENGE,
    CAST(WEMNG AS DECIMAL(13, 3)) AS WEMNG,

    {{ hashdiff(['EINDT', 'MENGE', 'WEMNG']) }} AS hashdiff_eket,

    'SAP_MM_EKET' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'eket') }}
