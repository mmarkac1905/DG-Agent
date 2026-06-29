/*
    Staging: SER03 — Serial Number Document Header (Material Document reference)
    1:1 with raw_sap.ser03.
*/

SELECT
    {{ hash_key(['MBLNR']) }} AS hk_material_document,
    {{ hash_key(['MATNR']) }} AS hk_material,

    OBKNR,
    OBZAE,
    SDESSION_TYPE,
    MBLNR,
    ZEILE,
    MATNR,
    CAST(MENGE AS DECIMAL(13, 3)) AS MENGE,
    MEINS,

    {{ hashdiff(['SDESSION_TYPE', 'MBLNR', 'ZEILE', 'MATNR', 'MENGE']) }} AS hashdiff_ser03,

    'SAP_MM_SER03' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'ser03') }}
