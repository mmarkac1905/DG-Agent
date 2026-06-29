/*
    Staging: VBRK — Billing Document Header (SD)
    1:1 with raw_sap.vbrk. One monthly service invoice per row; FKDAT is the billing
    date, FKSTO='X' marks a cancelled document. KUNRG is the payer.
*/

SELECT
    {{ hash_key(['VBELN']) }} AS hk_billing_doc,
    {{ hash_key(['KUNRG']) }} AS hk_customer,

    VBELN,
    FKART,
    VBTYP,
    CASE WHEN FKDAT IS NOT NULL AND FKDAT != '' AND LENGTH(FKDAT) = 8
        THEN CAST(SUBSTR(FKDAT, 1, 4) || '-' || SUBSTR(FKDAT, 5, 2) || '-' || SUBSTR(FKDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS FKDAT,
    CASE WHEN ERDAT IS NOT NULL AND ERDAT != '' AND LENGTH(ERDAT) = 8
        THEN CAST(SUBSTR(ERDAT, 1, 4) || '-' || SUBSTR(ERDAT, 5, 2) || '-' || SUBSTR(ERDAT, 7, 2) AS DATE)
        ELSE NULL
    END AS ERDAT,
    KUNRG,
    KUNAG,
    CAST(NETWR AS DECIMAL(15, 2)) AS NETWR,
    CAST(MWSBK AS DECIMAL(15, 2)) AS MWSBK,
    WAERK,
    VKORG,
    FKSTO,
    BUKRS,

    {{ hashdiff(['FKART', 'VBTYP', 'FKDAT', 'KUNRG', 'KUNAG', 'NETWR', 'MWSBK', 'WAERK', 'VKORG', 'FKSTO', 'BUKRS']) }} AS hashdiff_vbrk,

    'SAP_SD_VBRK' AS record_source,
    CURRENT_TIMESTAMP AS load_date

FROM {{ source('raw_sap', 'vbrk') }}
