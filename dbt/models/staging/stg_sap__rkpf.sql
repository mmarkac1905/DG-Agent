/*
    Staging: RKPF — Reservation Header
    1:1 with raw_sap.rkpf.
*/

SELECT
    {{ hash_key(['RSNUM']) }} AS hk_reservation,

    RSNUM,
    CASE WHEN RSDAT IS NOT NULL AND RSDAT != '' AND LENGTH(RSDAT) = 8
        THEN CAST(SUBSTR(RSDAT, 1, 4) || '-' || SUBSTR(RSDAT, 5, 2) || '-' || SUBSTR(RSDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS RSDAT,
    USNAM,
    BKTXT,

    {{ hashdiff(['RSDAT', 'USNAM', 'BKTXT']) }} AS hashdiff_rkpf,

    'SAP_MM_RKPF' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'rkpf') }}
