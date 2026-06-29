/*
    Staging: EKPO — Purchasing Document Item
    1:1 with raw_sap.ekpo. Note: hk_po_vendor is built at vault-time (requires EKKO join).
*/

SELECT
    {{ hash_key(['EBELN']) }} AS hk_purchase_order,
    {{ hash_key(['EBELN', 'EBELP']) }} AS hk_po_item,
    {{ hash_key(['MATNR']) }} AS hk_material,

    EBELN,
    EBELP,
    MATNR,
    TXZ01,
    CAST(MENGE AS DECIMAL(13, 3)) AS MENGE,
    MEINS,
    CAST(NETPR AS DECIMAL(11, 2)) AS NETPR,
    CAST(NETWR AS DECIMAL(13, 2)) AS NETWR,
    WERKS,
    LGORT,
    MATKL,
    PSTYP,
    BANFN,
    BNFPO,
    BPRME,
    ELIKZ,

    {{ hashdiff(['TXZ01', 'MENGE', 'MEINS', 'NETPR', 'NETWR', 'WERKS', 'LGORT', 'MATKL', 'PSTYP', 'ELIKZ']) }} AS hashdiff_ekpo,

    'SAP_MM_EKPO' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'ekpo') }}
