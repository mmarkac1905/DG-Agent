/*
    Staging: MAKT — Material Descriptions
    1:1 with raw_sap.makt.
*/

SELECT
    {{ hash_key(['MATNR']) }} AS hk_material,

    MATNR,
    SPRAS,
    MAKTX,

    {{ hashdiff(['MAKTX']) }} AS hashdiff_makt,

    'SAP_MM_MAKT' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'makt') }}
