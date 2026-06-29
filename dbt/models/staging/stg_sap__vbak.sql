/*
    Staging: VBAK — Sales Document Header (SD)
    1:1 with raw_sap.vbak. The service contract; KUNNR is the sold-to customer.
*/

SELECT
    {{ hash_key(['VBELN']) }} AS hk_sales_order,
    {{ hash_key(['KUNNR']) }} AS hk_customer,

    VBELN,
    CASE WHEN ERDAT IS NOT NULL AND ERDAT != '' AND LENGTH(ERDAT) = 8
        THEN CAST(SUBSTR(ERDAT, 1, 4) || '-' || SUBSTR(ERDAT, 5, 2) || '-' || SUBSTR(ERDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS ERDAT,
    CASE WHEN AUDAT IS NOT NULL AND AUDAT != '' AND LENGTH(AUDAT) = 8
        THEN CAST(SUBSTR(AUDAT, 1, 4) || '-' || SUBSTR(AUDAT, 5, 2) || '-' || SUBSTR(AUDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS AUDAT,
    AUART,
    VKORG,
    VTWEG,
    SPART,
    KUNNR,
    CAST(NETWR AS DECIMAL(15, 2)) AS NETWR,
    WAERK,

    {{ hashdiff(['AUDAT', 'AUART', 'VKORG', 'VTWEG', 'SPART', 'KUNNR', 'NETWR', 'WAERK']) }} AS hashdiff_vbak,

    'SAP_SD_VBAK' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'vbak') }}
