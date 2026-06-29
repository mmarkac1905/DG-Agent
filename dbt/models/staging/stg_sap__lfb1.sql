/*
    Staging: LFB1 — Vendor Company Code Data
    1:1 with raw_sap.lfb1.
*/

SELECT
    {{ hash_key(['LIFNR']) }} AS hk_vendor,

    LIFNR,
    BUKRS,
    ZTERM,
    AKONT,
    ZWELS,
    FDGRV,

    {{ hashdiff(['ZTERM', 'AKONT', 'ZWELS', 'FDGRV']) }} AS hashdiff_lfb1,

    'SAP_MM_LFB1' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'lfb1') }}
