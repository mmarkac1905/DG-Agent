/*
    Staging: MARM — Material Units of Measure
    1:1 with raw_sap.marm.
*/

SELECT
    {{ hash_key(['MATNR']) }} AS hk_material,

    MATNR,
    MEINH,
    CAST(UMREZ AS INTEGER) AS UMREZ,
    CAST(UMREN AS INTEGER) AS UMREN,

    {{ hashdiff(['MEINH', 'UMREZ', 'UMREN']) }} AS hashdiff_marm,

    'SAP_MM_MARM' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'marm') }}
