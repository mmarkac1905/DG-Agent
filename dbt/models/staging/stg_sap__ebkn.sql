/*
    Staging: EBKN — Purchase Requisition Account Assignment
    1:1 with raw_sap.ebkn.
*/

SELECT
    {{ hash_key(['BANFN']) }} AS hk_purchase_requisition,

    BANFN,
    BNFPO,
    SAKTO,
    KOSTL,

    {{ hashdiff(['SAKTO', 'KOSTL']) }} AS hashdiff_ebkn,

    'SAP_MM_EBKN' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'ebkn') }}
