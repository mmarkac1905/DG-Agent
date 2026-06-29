"""KI-117 — staging-/dimensional-layer panel state helpers.

Pure functions that compute compiled/deployed state without Streamlit
caching, so they're unit-testable independent of the Streamlit render
context. Used by Business_Glossary.py's S2T-Full-SQL panel to render
three states for each model:

  ✅ deployed (DuckDB)              — primary expected state
  ⚙️ compiled, not yet deployed     — pre-deployment workflow
  ❌ neither                         — truly unaccounted-for

Pre-fix the panel only consulted the filesystem compiled-SQL cache;
deployed-but-uncached models rendered as 'not yet compiled' next to
profile data showing 31,965 rows from the live DuckDB. KI-117 makes
DuckDB existence the primary check.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import duckdb


_LAYER_TO_SCHEMA = {
    "staging": "main_staging",
    "vault": "main_vault",
    "marts": "main_marts",
    "obt": "main_obt",
    "knowledge": "main_knowledge",
}


def load_deployed_keys(
    conn: duckdb.DuckDBPyConnection,
) -> set[tuple[str, str]]:
    """Snapshot every (table_schema, table_name) pair visible in DuckDB.

    Returns a set for O(1) membership tests across many model lookups.
    Defensive: any catalog access error returns an empty set, which
    causes every subsequent is_deployed() check to return False — the
    render then falls through to filesystem-only branches without
    masking the underlying error in the helper itself.
    """
    try:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables"
        ).fetchall()
    except Exception:
        return set()
    return {(s, n) for s, n in rows}


def is_deployed(
    model_name: str,
    layer: str,
    deployed_keys: set[tuple[str, str]],
) -> bool:
    """Pure: is (LAYER_TO_SCHEMA[layer], model_name) in the snapshot?

    Returns False for layers outside the dbt project (e.g. 'seeds') —
    those models live in main_seeds and aren't covered by the
    S2T-Full-SQL panel's _layer_order.
    """
    schema = _LAYER_TO_SCHEMA.get(layer)
    if not schema:
        return False
    return (schema, model_name) in deployed_keys


def read_compiled_sql(
    model_name: str, compiled_base: Path,
) -> Optional[str]:
    """Search compiled_base/<layer_dir>/<model_name>.sql for a file;
    return its contents or None if absent. Mirrors the pre-fix
    _read_compiled helper that lived inline in Business_Glossary.py."""
    if not compiled_base.exists():
        return None
    for layer_dir in compiled_base.iterdir():
        candidate = layer_dir / f"{model_name}.sql"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8")
    return None
