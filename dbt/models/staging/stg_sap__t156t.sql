/*
    Staging: T156T — Movement Type Text (SAP-native).
    1:1 with raw_sap.t156t. RULE 4 mechanical. One row per (BWART, SPRAS).
    SPRAS = SAP language code (E=English, H=Croatian).
*/
SELECT
    {{ hash_key(['BWART']) }} AS hk_movement_type,

    BWART,
    SPRAS,
    BTEXT,    -- short text
    LTEXT,    -- long text

    {{ hashdiff(['BTEXT', 'LTEXT']) }} AS hashdiff_t156t,

    'SAP_MM_T156T' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 't156t') }}
