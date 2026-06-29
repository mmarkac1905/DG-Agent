{#
    Vault helper macros for Data Vault 2.0 patterns.
    Used by hub, link, and satellite models.
#}

{%- macro vault_source_filter(src_alias, hash_key_col) -%}
{# Hub/Link incremental filter: only insert business keys not already loaded. #}
{% if is_incremental() %}
    WHERE {{ hash_key_col }} NOT IN (SELECT {{ hash_key_col }} FROM {{ this }})
{% endif %}
{%- endmacro -%}
