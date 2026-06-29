SELECT
    EKGRP, EKNAM,
    'SAP_MM_T024' AS record_source,
    CURRENT_TIMESTAMP AS load_date
FROM {{ source('raw_sap', 't024') }}
