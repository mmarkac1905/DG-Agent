{%- macro hash_key(columns) -%}
    MD5(CONCAT_WS('||', {%- for col in columns %}CAST(COALESCE(CAST({{ col }} AS VARCHAR), '') AS VARCHAR){% if not loop.last %}, {% endif %}{%- endfor %}))
{%- endmacro -%}

{%- macro hashdiff(columns) -%}
    MD5(CONCAT_WS('||', {%- for col in columns %}CAST(COALESCE(CAST({{ col }} AS VARCHAR), '') AS VARCHAR){% if not loop.last %}, {% endif %}{%- endfor %}))
{%- endmacro -%}
