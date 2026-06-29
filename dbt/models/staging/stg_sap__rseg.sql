/*
    Staging: RSEG — Invoice Document Item
    1:1 with raw_sap.rseg.
*/

SELECT
    {{ hash_key(['BELNR', 'GJAHR']) }} AS hk_invoice,
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,
    {{ hash_key(['MATNR']) }} AS hk_material,

    BELNR,
    GJAHR,
    BUZEI,
    EBELN,
    EBELP,
    MATNR,
    CAST(MENGE AS DECIMAL(13, 3)) AS MENGE,
    CAST(WRBTR AS DECIMAL(13, 2)) AS WRBTR,
    WAERS,

    {{ hashdiff(['EBELN', 'EBELP', 'MATNR', 'MENGE', 'WRBTR', 'WAERS']) }} AS hashdiff_rseg,

    'SAP_MM_RSEG' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'rseg') }}
