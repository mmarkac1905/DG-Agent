/*
    Staging: T156 — Movement Type Configuration (SAP-native).
    1:1 with raw_sap.t156. RULE 4 mechanical. Multi-language texts are
    in T156T (sat_movement_type_text), NOT here.
*/
SELECT
    {{ hash_key(['BWART']) }} AS hk_movement_type,

    BWART,
    BWARK,   -- movement category: A=receipt, B=issue, X=stock transfer
    KZBEW,   -- movement indicator: B=GR-for-PO, F=consumption, L=stock-transport, A=receipt-without-ref, X=initial
    SHKZG,   -- debit/credit: S=stock-account debit (synthetic: increase), H=credit (synthetic: decrease)

    {{ hashdiff(['BWARK', 'KZBEW', 'SHKZG']) }} AS hashdiff_t156,

    'SAP_MM_T156' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 't156') }}
