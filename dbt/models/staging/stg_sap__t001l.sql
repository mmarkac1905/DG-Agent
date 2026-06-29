SELECT
    {{ hash_key(['WERKS', 'LGORT']) }} AS hk_storage_location,
    {{ hash_key(['WERKS']) }} AS hk_plant,
    WERKS, LGORT, LGOBE,
    'SAP_MM_T001L' AS record_source,
    CURRENT_TIMESTAMP AS load_date
FROM {{ source('raw_sap', 't001l') }}
