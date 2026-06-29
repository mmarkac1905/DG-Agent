/*
    Staging: MARC — Material Plant Data
    1:1 with raw_sap.marc.
*/

SELECT
    {{ hash_key(['MATNR']) }} AS hk_material,
    {{ hash_key(['WERKS']) }} AS hk_plant,
    {{ hash_key(['MATNR', 'WERKS']) }} AS hk_material_plant,

    MATNR,
    WERKS,
    DISMM,
    DISPO,
    EKGRP,
    BESKZ,
    SOBSL,
    LGPRO,
    PLIFZ,
    SERNP,

    {{ hashdiff(['DISMM', 'DISPO', 'EKGRP', 'BESKZ', 'SOBSL', 'LGPRO', 'PLIFZ', 'SERNP']) }} AS hashdiff_marc,

    'SAP_MM_MARC' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'marc') }}
