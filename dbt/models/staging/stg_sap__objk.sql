/*
    Staging: OBJK — Object List (Serial to Equipment cross-reference)
    1:1 with raw_sap.objk.
*/

SELECT
    {{ hash_key(['EQUNR']) }} AS hk_equipment,
    {{ hash_key(['MATNR']) }} AS hk_material,

    OBKNR,
    OBZAE,
    OBTYP,
    OBJNR,
    MATNR,
    SERNR,
    TASER,
    EQUNR,

    {{ hashdiff(['OBTYP', 'OBJNR', 'MATNR', 'SERNR', 'TASER']) }} AS hashdiff_objk,

    'SAP_PM_OBJK' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'objk') }}
