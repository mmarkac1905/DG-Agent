/*
    Staging: SERI — Serial Number Assignments
    1:1 with raw_sap.seri.
*/

SELECT
    {{ hash_key(['EQUNR']) }} AS hk_equipment,
    {{ hash_key(['MATNR']) }} AS hk_material,

    OBKNR,
    MBLNR,
    ZEILE,
    ACCESSION_DATE,
    SERNR,
    MATNR,
    EQUNR,

    {{ hashdiff(['MBLNR', 'ZEILE', 'SERNR', 'MATNR']) }} AS hashdiff_seri,

    'SAP_MM_SERI' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'seri') }}
