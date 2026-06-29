/*
    Staging: MSEG — Material Document Item
    1:1 with raw_sap.mseg.
*/

SELECT
    {{ hash_key(['MBLNR', 'MJAHR']) }} AS hk_material_document,
    {{ hash_key(['MATNR']) }} AS hk_material,
    {{ hash_key(['WERKS']) }} AS hk_plant,
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,

    MBLNR,
    MJAHR,
    ZEILE,
    BWART,
    MATNR,
    WERKS,
    LGORT,
    CAST(MENGE AS DECIMAL(13, 3)) AS MENGE,
    MEINS,
    EBELN,
    EBELP,
    CAST(DMBTR AS DECIMAL(13, 2)) AS DMBTR,
    WAERS,
    SERNP,
    LIFNR,

    {{ hashdiff(['BWART', 'MATNR', 'WERKS', 'LGORT', 'MENGE', 'MEINS', 'DMBTR', 'WAERS', 'SERNP', 'LIFNR']) }} AS hashdiff_mseg,

    'SAP_MM_MSEG' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'mseg') }}
