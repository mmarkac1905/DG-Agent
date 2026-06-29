"""Stage D.1 — shared helpers extracted from Data_Analysis.py during
legacy cleanup.

Previously module-level in `app/pages/Data_Analysis.py`, these helpers
are consumed by multiple tab blocks (tab_explore, tab_report, plus the
deleted Section 2 flows of tab_guided + tab_domain). Extracting here
keeps the shared surface intact after legacy deletion.

Import convention: `from _data_analysis_shared import ...` (app/ is
sys.path root via Streamlit page discovery, NOT a package).
"""
from __future__ import annotations

import re

from db import query


def rewrite_sql_for_staging(sql_text: str) -> str:
    """Rewrite `raw_sap.<table>` references to `main_staging.stg_sap__<lowercase>`.

    Claude sometimes falls back to the raw_sap naming even though we tell it to
    use main_staging. A regex pass is safer than string replacement — it handles
    aliases (``raw_sap.EKKO e``), mixed case, and subqueries in one shot.
    """
    if not sql_text:
        return sql_text
    return re.sub(
        r'raw_sap\.(\w+)',
        lambda m: f"main_staging.stg_sap__{m.group(1).lower()}",
        sql_text,
        flags=re.IGNORECASE,
    )


def run_query_with_fallback(conn, sql_text: str):
    """Execute `sql_text` against DuckDB with a 3-step fallback:

      1. Rewrite raw_sap.* → main_staging.stg_sap__*  (most common case)
      2. Try the unmodified SQL (in case the user already used main_staging)
      3. Try rewriting raw_sap.* → main_marts.*  (sometimes Claude aims at marts)

    Returns `(dataframe, error_message)` where exactly one of the two is
    non-None. Caller decides how to render the error.
    """
    attempts = [
        rewrite_sql_for_staging(sql_text),
        sql_text,
        re.sub(
            r'raw_sap\.(\w+)',
            lambda m: f"main_marts.{m.group(1).lower()}",
            sql_text,
            flags=re.IGNORECASE,
        ),
    ]
    last_err = None
    seen = set()
    for attempt in attempts:
        if attempt in seen:
            continue
        seen.add(attempt)
        try:
            return conn.execute(attempt).fetchdf(), None
        except Exception as e:  # noqa: BLE001
            last_err = e
    return None, str(last_err) if last_err else "Query failed"


def load_actual_staging_schema() -> str:
    """Return the real main_staging schema as a CSV string.

    Filters out DV housekeeping columns (record_source, load_date, hashdiff,
    hk_*) so Claude sees only the business-meaningful columns that actually
    exist in our warehouse — not the theoretical SAP data dictionary.
    """
    try:
        df = query(
            """
            SELECT
                REPLACE(table_name, 'stg_sap__', '') AS sap_table,
                column_name,
                data_type
            FROM information_schema.columns
            WHERE table_schema = 'main_staging'
              AND column_name NOT IN ('record_source', 'load_date')
              AND column_name NOT LIKE 'hashdiff%'
              AND column_name NOT LIKE 'hk_%'
            ORDER BY table_name, ordinal_position
            """
        )
        return df.to_csv(index=False)
    except Exception:
        return ""
