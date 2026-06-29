/*
    Staging: MARD — Stock per Storage Location
    1:1 with raw_sap.mard.
*/

SELECT
    {{ hash_key(['MATNR']) }} AS hk_material,
    {{ hash_key(['WERKS']) }} AS hk_plant,
    {{ hash_key(['MATNR', 'WERKS', 'LGORT']) }} AS hk_stock_location,

    MATNR,
    WERKS,
    LGORT,
    CAST(LABST AS DECIMAL(13, 3)) AS LABST,
    CAST(INSME AS DECIMAL(13, 3)) AS INSME,
    CAST(SPEME AS DECIMAL(13, 3)) AS SPEME,

    {{ hashdiff(['LABST', 'INSME', 'SPEME']) }} AS hashdiff_mard,

    'SAP_MM_MARD' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'mard') }}
