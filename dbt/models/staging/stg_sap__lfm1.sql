/*
    Staging: LFM1 — Vendor Purchasing Organization Data
    1:1 with raw_sap.lfm1.
*/

SELECT
    {{ hash_key(['LIFNR']) }} AS hk_vendor,

    LIFNR,
    EKORG,
    WAERS,
    ZTERM,
    WEBRE,
    LEBRE,

    {{ hashdiff(['WAERS', 'ZTERM', 'WEBRE', 'LEBRE']) }} AS hashdiff_lfm1,

    'SAP_MM_LFM1' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'lfm1') }}
