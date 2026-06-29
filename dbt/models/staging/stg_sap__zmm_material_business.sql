/*
    Staging: ZMM_MATERIAL_BUSINESS — HT-domain material business enrichment.
    1:1 with raw_sap.zmm_material_business. Hash keys + hashdiff per RULE 4
    (mechanical layer, no business logic). Attaches to hub_material via
    hk_material (hash of MATNR).
*/

SELECT
    {{ hash_key(['MATNR']) }} AS hk_material,

    MATNR,
    CAST(LIFECYCLE_MONTHS AS INTEGER) AS LIFECYCLE_MONTHS,
    PRIMARY_VENDOR_ID,
    NOTES,

    {{ hashdiff(['LIFECYCLE_MONTHS', 'PRIMARY_VENDOR_ID', 'NOTES']) }} AS hashdiff_zmm_material_business,

    'SAP_MM_ZMM_MATERIAL_BUSINESS' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'zmm_material_business') }}
