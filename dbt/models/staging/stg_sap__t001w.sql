SELECT
    {{ hash_key(['WERKS']) }} AS hk_plant,
    WERKS, NAME1, ORT01, LAND1, BUKRS,
    'SAP_MM_T001W' AS record_source,
    CURRENT_TIMESTAMP AS load_date
FROM {{ source('raw_sap', 't001w') }}
