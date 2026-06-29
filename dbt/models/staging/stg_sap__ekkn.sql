/*
    Staging: EKKN — Purchasing Document Account Assignment
    1:1 with raw_sap.ekkn.
*/

SELECT
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,
    {{ hash_key(['EBELN', 'EBELP']) }} AS hk_po_item,

    EBELN,
    EBELP,
    ZEKKN,
    SAKTO,
    KOSTL,

    {{ hashdiff(['SAKTO', 'KOSTL']) }} AS hashdiff_ekkn,

    'SAP_MM_EKKN' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'ekkn') }}
