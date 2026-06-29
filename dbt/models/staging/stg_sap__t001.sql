SELECT
    {{ hash_key(['BUKRS']) }} AS hk_company_code,
    BUKRS, BUTXT, ORT01, LAND1, WAERS, SPRAS,
    'SAP_FI_T001' AS record_source,
    CURRENT_TIMESTAMP AS load_date
FROM {{ source('raw_sap', 't001') }}
