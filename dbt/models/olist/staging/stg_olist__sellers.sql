{{ config(materialized='view') }}

-- Auto-staged 1:1 passthrough: generated deterministically because
-- this table entered a term's confirmed scope before any richer
-- staging model existed (see scripts/_staging_bootstrap.py).
SELECT
    "seller_id",
    "seller_zip_code_prefix",
    "seller_city",
    "seller_state"
FROM {{ source('raw_olist', 'sellers') }}
