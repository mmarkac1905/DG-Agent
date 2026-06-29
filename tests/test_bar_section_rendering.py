"""KI-103 hardening — render_bar_section bar_id parameter tests.

Verifies the C6 UI fix: when render_bar_section is called with an
explicit bar_id (e.g., from the dispatcher refusal output), it
bypasses the parquet-backed `query` callable and reads directly from
cpe_analytics.duckdb. This makes the refusal-path renderer wire to
the dispatcher's id, not parquet's latest — robust against parquet
staleness (which surfaced as the 2026-04-29 manual UI walk bug:
dispatcher cited BAR-00010 but renderer showed BAR-00006 because
parquet hadn't synced past BAR-00006).

Tests target the helper `_fetch_bar_row`, which is the substantive
data-source logic; rendering is just streamlit calls that are tested
via Streamlit AppTest elsewhere.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))

import _bar_section  # noqa: E402


def _setup_test_db(tmp_path: Path, rows: list[tuple]) -> Path:
    """Build a minimal main_seeds.business_term_analysis_results table
    with the columns _fetch_bar_row's bar_id query touches."""
    db_path = tmp_path / "cpe_analytics.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.business_term_analysis_results (
            id VARCHAR,
            business_term_id VARCHAR,
            status VARCHAR,
            convergence_reason VARCHAR
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO main_seeds.business_term_analysis_results VALUES "
            "(?, ?, ?, ?)",
            list(r),
        )
    conn.close()
    return db_path


def _patch_duckdb_to(test_db: Path, monkeypatch) -> None:
    """Redirect any duckdb.connect call inside _bar_section to the test
    DB. _fetch_bar_row hardcodes a path; monkeypatching `duckdb.connect`
    on the module's global lets us intercept without changing signature."""
    real_connect = duckdb.connect

    def _fake_connect(_path, **kw):  # noqa: ARG001
        return real_connect(str(test_db), **kw)

    # _fetch_bar_row does `import duckdb` inside the function, so the
    # monkeypatch needs to land on the duckdb module itself.
    monkeypatch.setattr(duckdb, "connect", _fake_connect)


def test_render_bar_section_with_bar_id_uses_live_duckdb(
    tmp_path, monkeypatch,
) -> None:
    """When bar_id is provided, _fetch_bar_row reads from DuckDB and the
    parquet-backed `query` callable is never invoked."""
    db_path = _setup_test_db(tmp_path, [
        ("BAR-LIVE", "BG-T", "needs_data_extension",
         "hard_stop_bridge_unreachable"),
    ])
    _patch_duckdb_to(db_path, monkeypatch)
    query_mock = MagicMock()  # would error if called

    bar = _bar_section._fetch_bar_row(
        term_id="BG-T", query=query_mock, bar_id="BAR-LIVE",
    )

    assert bar is not None
    assert bar["id"] == "BAR-LIVE"
    assert bar["status"] == "needs_data_extension"
    assert query_mock.call_count == 0


def test_render_bar_section_without_bar_id_uses_parquet_fallback(
    tmp_path, monkeypatch,
) -> None:
    """When bar_id is None, _fetch_bar_row uses the parquet-backed
    `query` callable. DuckDB is not opened directly."""
    # Sentinel — if duckdb.connect is invoked inadvertently, we want to
    # know. Patch with a raiser.
    def _no_duckdb(_path, **kw):  # noqa: ARG001
        raise AssertionError(
            "duckdb.connect should NOT be called when bar_id is None"
        )

    monkeypatch.setattr(duckdb, "connect", _no_duckdb)

    parquet_df = pd.DataFrame([{
        "id": "BAR-PARQUET",
        "business_term_id": "BG-T",
        "status": "converged",
        "convergence_reason": "converged_soft",
    }])
    query_mock = MagicMock(return_value=parquet_df)

    bar = _bar_section._fetch_bar_row(
        term_id="BG-T", query=query_mock, bar_id=None,
    )

    assert bar is not None
    assert bar["id"] == "BAR-PARQUET"
    assert query_mock.call_count == 1
    invoked_sql = query_mock.call_args[0][0]
    assert "ORDER BY executed_at_utc DESC" in invoked_sql
    assert "BG-T" in invoked_sql


def test_render_bar_section_with_invalid_bar_id_returns_none(
    tmp_path, monkeypatch,
) -> None:
    """When bar_id refers to a row not in DuckDB, _fetch_bar_row returns
    None (renderer surfaces st.error to the caller)."""
    db_path = _setup_test_db(tmp_path, [
        ("BAR-EXISTING", "BG-T", "converged", "converged_soft"),
    ])
    _patch_duckdb_to(db_path, monkeypatch)
    query_mock = MagicMock()

    bar = _bar_section._fetch_bar_row(
        term_id="BG-T", query=query_mock, bar_id="BAR-DOES-NOT-EXIST",
    )

    assert bar is None
    assert query_mock.call_count == 0  # no fallback to parquet on miss


def test_dispatch_forwards_bar_id_to_renderer() -> None:
    """The C6 refusal-kind dispatch in Business_Glossary.py forwards the
    dispatcher's _bar_id to render_bar_section. Source-string check —
    the alternative is a heavy Streamlit AppTest, but the substantive
    contract is one line at the call site."""
    src = (
        _ROOT / "app" / "pages" / "Business_Glossary.py"
    ).read_text(encoding="utf-8")
    # Refusal branch must forward _bar_id to render_bar_section
    assert "_refusal_kind\") == \"bar_needs_data_extension\"" in src
    assert "render_bar_section(" in src
    # The forward must use bar_id keyword + result.get("_bar_id")
    assert 'bar_id=result.get("_bar_id")' in src


def test_render_bar_section_signature_accepts_bar_id() -> None:
    """Public signature smoke check — bar_id is a keyword-only-friendly
    optional parameter so existing call sites (passing only term_id +
    query) continue to work."""
    import inspect
    sig = inspect.signature(_bar_section.render_bar_section)
    assert "bar_id" in sig.parameters
    assert sig.parameters["bar_id"].default is None
