/*
    Staging: RBKP — Invoice Document Header
    1:1 with raw_sap.rbkp.
*/

SELECT
    {{ hash_key(['BELNR', 'GJAHR']) }} AS hk_invoice,
    {{ hash_key(['LIFNR']) }} AS hk_vendor,
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,

    BELNR,
    GJAHR,
    CASE WHEN BLDAT IS NOT NULL AND BLDAT != '' AND LENGTH(BLDAT) = 8
        THEN CAST(SUBSTR(BLDAT, 1, 4) || '-' || SUBSTR(BLDAT, 5, 2) || '-' || SUBSTR(BLDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BLDAT,
    CASE WHEN BUDAT IS NOT NULL AND BUDAT != '' AND LENGTH(BUDAT) = 8
        THEN CAST(SUBSTR(BUDAT, 1, 4) || '-' || SUBSTR(BUDAT, 5, 2) || '-' || SUBSTR(BUDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS BUDAT,
    LIFNR,
    WAERS,
    CAST(RMWWR AS DECIMAL(13, 2)) AS RMWWR,
    XBLNR,
    EBELN,
    USNAM,

    {{ hashdiff(['BLDAT', 'BUDAT', 'LIFNR', 'WAERS', 'RMWWR', 'XBLNR', 'EBELN']) }} AS hashdiff_rbkp,

    'SAP_MM_RBKP' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'rbkp') }}
