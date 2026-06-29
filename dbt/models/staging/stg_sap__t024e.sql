SELECT
    EKORG, EKOTX, BUKRS, LAND1,
    'SAP_MM_T024E' AS record_source,
    CURRENT_TIMESTAMP AS load_date
FROM {{ source('raw_sap', 't024e') }}
