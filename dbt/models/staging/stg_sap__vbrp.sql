/*
    Staging: VBRP — Billing Document Item (SD)
    1:1 with raw_sap.vbrp. NETWR is the service revenue; MATNR is the service plan;
    AUBEL references the originating sales order (VBAK).
*/

SELECT
    {{ hash_key(['VBELN']) }} AS hk_billing_doc,
    {{ hash_key(['VBELN', 'POSNR']) }} AS hk_billing_item,
    {{ hash_key(['MATNR']) }} AS hk_service_plan,
    {{ hash_key(['AUBEL']) }} AS hk_sales_order,

    VBELN,
    POSNR,
    CAST(FKIMG AS DECIMAL(13, 3)) AS FKIMG,
    VRKME,
    CAST(NETWR AS DECIMAL(15, 2)) AS NETWR,
    MATNR,
    ARKTX,
    MATKL,
    WERKS,
    AUBEL,
    AUPOS,
    CASE WHEN PRSDT IS NOT NULL AND PRSDT != '' AND LENGTH(PRSDT) = 8
        THEN CAST(SUBSTR(PRSDT, 1, 4) || '-' || SUBSTR(PRSDT, 5, 2) || '-' || SUBSTR(PRSDT, 7, 2) AS DATE)
        ELSE NULL
    END AS PRSDT,

    {{ hashdiff(['FKIMG', 'VRKME', 'NETWR', 'MATNR', 'ARKTX', 'MATKL', 'WERKS', 'AUBEL', 'AUPOS', 'PRSDT']) }} AS hashdiff_vbrp,

    'SAP_SD_VBRP' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'vbrp') }}
