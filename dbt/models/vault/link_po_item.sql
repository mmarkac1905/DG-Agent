{{
    config(
        materialized='incremental',
        unique_key='hk_po_item'
    )
}}

/*
    Link: Purchase Order Item
    Connects a PO item to its parent PO.
    Business key: EBELN + EBELP
    Source: stg_sap__ekpo
*/

WITH src AS (
    SELECT DISTINCT
        hk_po_item,
        hk_purchase_order,
        EBELN AS purchase_order_number,
        EBELP AS po_item_number,
        load_date,
        record_source
    FROM {{ ref('stg_sap__ekpo') }}
)

SELECT * FROM src
{% if is_incremental() %}
WHERE hk_po_item NOT IN (SELECT hk_po_item FROM {{ this }})
{% endif %}
