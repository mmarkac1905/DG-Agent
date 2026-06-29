{{ config(materialized='incremental', unique_key='hk_zmm_approval') }}

/*
    Hub: ZMM Approval (custom Z-table approval events)
    Business key: APPROVAL_ID (unique per approval event)
    Source: stg_sap__zmm_approval_log
*/

SELECT DISTINCT
    hk_zmm_approval,
    APPROVAL_ID AS approval_id,
    load_date,
    record_source
FROM {{ ref('stg_sap__zmm_approval_log') }} src
{{ vault_source_filter('src', 'hk_zmm_approval') }}
