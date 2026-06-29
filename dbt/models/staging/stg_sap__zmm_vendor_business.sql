/*
    Staging: ZMM_VENDOR_BUSINESS — HT-domain vendor business enrichment.
    1:1 with raw_sap.zmm_vendor_business. Hash keys + hashdiff per RULE 4
    (mechanical layer, no business logic). Attaches to hub_vendor via
    hk_vendor (hash of LIFNR).
*/

SELECT
    {{ hash_key(['LIFNR']) }} AS hk_vendor,

    LIFNR,
    EQUIPMENT_TYPES,
    CONTRACT_STATUS,
    QUALITY_RATING,
    NOTES,

    {{ hashdiff(['EQUIPMENT_TYPES', 'CONTRACT_STATUS', 'QUALITY_RATING', 'NOTES']) }} AS hashdiff_zmm_vendor_business,

    'SAP_MM_ZMM_VENDOR_BUSINESS' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'zmm_vendor_business') }}
