SELECT
    MATKL, WGBEZ,
    'SAP_MM_T023' AS record_source,
    CURRENT_TIMESTAMP AS load_date
FROM {{ source('raw_sap', 't023') }}
