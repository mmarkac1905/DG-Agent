"""KI-117 — tests for the compiled-state helpers backing the
S2T-Full-SQL panel's three-state render.

The render itself is hard to unit-test (Streamlit context); the
brief approves extracting the seam to pure helpers and testing
those — see app/_compiled_state.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pytest

# Tests live in tests/, helper lives in app/_compiled_state.py;
# add app/ to sys.path so the helper imports without standing up
# Streamlit.
_APP = Path(__file__).resolve().parent.parent / "app"
if str(_APP) not in sys.path:
    sys.path.insert(0, str(_APP))

from _compiled_state import (  # noqa: E402
    is_deployed,
    load_deployed_keys,
    read_compiled_sql,
)


def test_is_deployed_returns_true_for_deployed_model() -> None:
    """Pre-fix UI showed 'not yet compiled' for stg_sap__mseg even
    though main_staging.stg_sap__mseg was deployed; this check is
    the primary signal that closed the gap."""
    keys = {("main_staging", "stg_sap__mseg")}
    assert is_deployed("stg_sap__mseg", "staging", keys) is True


def test_is_deployed_returns_false_for_undeployed() -> None:
    """Model name absent from snapshot (or wrong layer) → False;
    render falls through to filesystem-only branches."""
    keys = {("main_staging", "stg_sap__mseg")}
    assert is_deployed("stg_sap__never_built", "staging", keys) is False
    # Right name, wrong layer → wrong schema lookup → False.
    assert is_deployed("stg_sap__mseg", "marts", keys) is False
    # Layer outside the dbt project's _layer_order → False (defensive
    # against future _layer_order additions without a schema mapping).
    assert is_deployed("anything", "seeds", keys) is False


def test_load_deployed_keys_returns_pairs_for_real_tables() -> None:
    """In-memory DuckDB with a couple of schemas + tables; helper
    should return the (schema, table) tuple set."""
    conn = duckdb.connect(":memory:")
    try:
        conn.execute("CREATE SCHEMA main_staging")
        conn.execute("CREATE SCHEMA main_marts")
        conn.execute("CREATE TABLE main_staging.stg_sap__mseg (id INTEGER)")
        conn.execute("CREATE TABLE main_staging.stg_sap__mkpf (id INTEGER)")
        conn.execute("CREATE TABLE main_marts.fact_x (id INTEGER)")
        keys = load_deployed_keys(conn)
        assert ("main_staging", "stg_sap__mseg") in keys
        assert ("main_staging", "stg_sap__mkpf") in keys
        assert ("main_marts", "fact_x") in keys
    finally:
        conn.close()


def test_load_deployed_keys_handles_catalog_error_gracefully() -> None:
    """Closed connection → catalog query raises → helper returns
    empty set (defensive). The render then treats every model as
    'not in DuckDB' and falls through to filesystem-only state."""
    conn = duckdb.connect(":memory:")
    conn.close()
    assert load_deployed_keys(conn) == set()


def test_read_compiled_sql_returns_content_when_file_exists(
    tmp_path: Path,
) -> None:
    """Helper walks compiled_base subdirs looking for <model>.sql;
    returns file contents when found."""
    layer = tmp_path / "staging"
    layer.mkdir()
    f = layer / "stg_sap__mseg.sql"
    f.write_text("SELECT 1 AS sentinel", encoding="utf-8")
    assert read_compiled_sql("stg_sap__mseg", tmp_path) == "SELECT 1 AS sentinel"


def test_read_compiled_sql_returns_none_when_absent(
    tmp_path: Path,
) -> None:
    """Empty compiled_base, or model not in any subdir → None.
    Pre-fix this triggered 'not yet compiled' for deployed models;
    post-fix the render distinguishes 'deployed without cache' (the
    common case for stg_sap__mseg/mkpf as observed) from 'compiled,
    not deployed' (pre-deployment workflow)."""
    # Empty base dir.
    assert read_compiled_sql("anything", tmp_path) is None
    # Subdir exists but file doesn't.
    (tmp_path / "staging").mkdir()
    assert read_compiled_sql("stg_sap__mseg", tmp_path) is None
    # Non-existent base path.
    assert read_compiled_sql("anything", tmp_path / "does_not_exist") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
