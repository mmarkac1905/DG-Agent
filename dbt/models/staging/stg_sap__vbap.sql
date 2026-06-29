/*
    Staging: VBAP — Sales Document Item (SD)
    1:1 with raw_sap.vbap. MATNR is the service plan; SERNR is the deployed CPE
    device serial (the tie from service revenue to the physical router — resolved
    to the equipment hub at vault time, never joined here).
*/

SELECT
    {{ hash_key(['VBELN']) }} AS hk_sales_order,
    {{ hash_key(['VBELN', 'POSNR']) }} AS hk_sales_order_item,
    {{ hash_key(['MATNR']) }} AS hk_service_plan,

    VBELN,
    POSNR,
    MATNR,
    ARKTX,
    MATKL,
    CAST(KWMENG AS DECIMAL(13, 3)) AS KWMENG,
    VRKME,
    CAST(NETWR AS DECIMAL(15, 2)) AS NETWR,
    WERKS,
    SERNR,

    {{ hashdiff(['MATNR', 'ARKTX', 'MATKL', 'KWMENG', 'VRKME', 'NETWR', 'WERKS', 'SERNR']) }} AS hashdiff_vbap,

    'SAP_SD_VBAP' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'vbap') }}
