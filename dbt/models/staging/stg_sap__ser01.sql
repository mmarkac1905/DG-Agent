/*
    Staging: SER01 — Serial Number Document Header (PO reference)
    1:1 with raw_sap.ser01.
*/

SELECT
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,
    {{ hash_key(['MATNR']) }} AS hk_material,

    OBKNR,
    OBZAE,
    SDESSION_TYPE,
    EBELN,
    EBELP,
    MATNR,
    CAST(MENGE AS DECIMAL(13, 3)) AS MENGE,
    MEINS,

    {{ hashdiff(['SDESSION_TYPE', 'EBELN', 'EBELP', 'MATNR', 'MENGE']) }} AS hashdiff_ser01,

    'SAP_MM_SER01' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'ser01') }}
