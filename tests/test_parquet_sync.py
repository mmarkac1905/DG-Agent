"""Tests for scripts/_parquet_sync.sync_parquet_and_invalidate (known_issue #81).

The pre-#81 implementation spawned `dbt seed` as a subprocess; on
Windows, that subprocess failed with an exclusive-file-lock IO Error
whenever the parent Python process held an open read-write conn to
cpe_analytics.duckdb. The lock failure was silent (caught + warning
to stderr + returned early). #81 replaced the subprocess with an
in-process CREATE OR REPLACE TABLE via the caller's conn (or a
short-lived writer when no conn is passed).

Tests cover:
  1. Parent conn open + sync works — regression guard for today's
     run_term_eda.py:824 failure case.
  2. Typed seed DDL enforces schema.yml column_types (guards #73).
  3. No subprocess spawned (explicit guarantee).
  4. Parquet file written with all rows.
  5a. Untyped-seed auto-inference fallback (column count).
  5b. Untyped-seed per-column type assertions (Addition 1).
"""
from __future__ import annotations

import csv
import sys
import textwrap
from pathlib import Path

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _parquet_sync as mod  # noqa: E402


def _prepare_project_root(tmp_path: Path) -> Path:
    """Build a minimal project skeleton: dbt/seeds/, data/parquet/,
    cpe_analytics.duckdb."""
    (tmp_path / "dbt" / "seeds").mkdir(parents=True)
    (tmp_path / "data" / "parquet" / "main_seeds").mkdir(parents=True)
    # Touch empty DuckDB so the writer can open/attach.
    db = tmp_path / "cpe_analytics.duckdb"
    duckdb.connect(str(db)).close()
    return tmp_path


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)


def _install_fake_schema_types(
    monkeypatch, mapping: dict[str, dict[str, str]],
) -> None:
    """Replace the lru_cache'd loader so tests see our fixture types."""
    mod._load_schema_column_types.cache_clear()
    monkeypatch.setattr(mod, "_load_schema_column_types", lambda: mapping)


# =========================================================================
# Test 1 — the exact run_term_eda.py:824 failure pattern
# =========================================================================

def test_sync_succeeds_with_parent_conn_held_open(tmp_path, monkeypatch):
    """Regression for known_issue #81 root case.

    Caller holds a read-write conn to cpe_analytics.duckdb; calls
    sync_parquet_and_invalidate with conn=<parent>. Pre-#81-fix this
    would have spawned `dbt seed` subprocess which failed with a
    Windows exclusive-file-lock IO Error. Post-fix the helper writes
    in-process via the caller's conn — no subprocess, no lock.
    """
    project_root = _prepare_project_root(tmp_path)
    seed_csv = project_root / "dbt" / "seeds" / "tar_test.csv"
    _write_csv(seed_csv, ["id", "name"], [["1", "a"], ["2", "b"], ["3", "c"]])
    _install_fake_schema_types(monkeypatch, {
        "tar_test": {"id": "varchar", "name": "varchar"},
    })

    parent = duckdb.connect(str(project_root / "cpe_analytics.duckdb"),
                            read_only=False)
    try:
        warning = mod.sync_parquet_and_invalidate(
            project_root=project_root,
            seed_name="tar_test",
            source="test",
            conn=parent,
        )
        assert warning is None, f"expected no warning; got {warning!r}"
        n = parent.execute(
            'SELECT COUNT(*) FROM main_seeds."tar_test"'
        ).fetchone()[0]
        assert n == 3, f"expected 3 rows loaded; got {n}"
    finally:
        parent.close()


# =========================================================================
# Test 2 — typed seed DDL enforcement (guards #73 class)
# =========================================================================

def test_typed_seed_creates_column_types_from_schema_yml(tmp_path, monkeypatch):
    """Regression for known_issue #73 class.

    A column declared VARCHAR in schema.yml must be VARCHAR in the
    DuckDB table even when the CSV's column values would sniff as
    INTEGER (e.g., all-empty + numeric-only strings). Pre-#73-fix,
    dbt seed sniffed INTEGER; post-fix + Option A, explicit DDL forces
    VARCHAR regardless.
    """
    project_root = _prepare_project_root(tmp_path)
    seed_csv = project_root / "dbt" / "seeds" / "dar_test.csv"
    # All values for "superseded_by" are empty — pure-inference would
    # pick INTEGER/NULL type. Schema.yml declares VARCHAR.
    _write_csv(seed_csv, ["id", "superseded_by"], [
        ["DAR-01", ""],
        ["DAR-02", ""],
        ["DAR-03", ""],
    ])
    _install_fake_schema_types(monkeypatch, {
        "dar_test": {"id": "varchar", "superseded_by": "varchar"},
    })

    parent = duckdb.connect(str(project_root / "cpe_analytics.duckdb"),
                            read_only=False)
    try:
        warning = mod.sync_parquet_and_invalidate(
            project_root=project_root,
            seed_name="dar_test",
            source="test",
            conn=parent,
        )
        assert warning is None

        col_info = parent.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main_seeds' AND table_name = 'dar_test' "
            "ORDER BY ordinal_position"
        ).fetchall()
        types = dict(col_info)
        assert types["id"] == "VARCHAR"
        assert types["superseded_by"] == "VARCHAR", (
            f"superseded_by must be VARCHAR per schema.yml column_types; "
            f"got {types.get('superseded_by')}"
        )
    finally:
        parent.close()


# =========================================================================
# Test 3 — no subprocess spawn
# =========================================================================

def test_sync_does_not_spawn_subprocess(tmp_path, monkeypatch):
    """Explicit guarantee: the Option A helper must never call
    subprocess.run on a seed_name-provided invocation. Pre-#81 the
    entire failure mode came from subprocess dbt seed; post-fix that
    path is gone for normal calls."""
    project_root = _prepare_project_root(tmp_path)
    seed_csv = project_root / "dbt" / "seeds" / "ts_test.csv"
    _write_csv(seed_csv, ["id"], [["1"], ["2"]])
    _install_fake_schema_types(monkeypatch, {
        "ts_test": {"id": "varchar"},
    })

    def _boom(*args, **kwargs):
        raise RuntimeError("subprocess.run must not be invoked by Option A")
    monkeypatch.setattr(mod.subprocess, "run", _boom)

    parent = duckdb.connect(str(project_root / "cpe_analytics.duckdb"),
                            read_only=False)
    try:
        warning = mod.sync_parquet_and_invalidate(
            project_root=project_root,
            seed_name="ts_test",
            source="test",
            conn=parent,
        )
        assert warning is None
    finally:
        parent.close()


# =========================================================================
# Test 4 — parquet file written
# =========================================================================

def test_parquet_file_written_with_current_rows(tmp_path, monkeypatch):
    """After a successful sync, data/parquet/main_seeds/<seed>.parquet
    exists and contains the same row count as the CSV. This is how
    Streamlit sees the fresh data — parquet is the read layer."""
    project_root = _prepare_project_root(tmp_path)
    seed_csv = project_root / "dbt" / "seeds" / "pq_test.csv"
    _write_csv(seed_csv, ["k", "v"], [
        ["a", "1"], ["b", "2"], ["c", "3"], ["d", "4"],
    ])
    _install_fake_schema_types(monkeypatch, {
        "pq_test": {"k": "varchar", "v": "integer"},
    })

    parent = duckdb.connect(str(project_root / "cpe_analytics.duckdb"),
                            read_only=False)
    try:
        warning = mod.sync_parquet_and_invalidate(
            project_root=project_root,
            seed_name="pq_test",
            source="test",
            conn=parent,
        )
        assert warning is None
    finally:
        parent.close()

    pq_path = project_root / "data" / "parquet" / "main_seeds" / "pq_test.parquet"
    assert pq_path.exists(), f"expected parquet at {pq_path}; not found"

    # Fresh in-memory conn to read the parquet back — proves it's valid
    # and row count matches the CSV.
    tmp_conn = duckdb.connect(":memory:")
    n = tmp_conn.execute(
        f"SELECT COUNT(*) FROM read_parquet('{pq_path.as_posix()}')"
    ).fetchone()[0]
    assert n == 4, f"parquet row count mismatch; expected 4, got {n}"
    tmp_conn.close()


# =========================================================================
# Test 5a — untyped-seed auto-inference produces columns
# =========================================================================

def test_untyped_seed_uses_read_csv_auto(tmp_path, monkeypatch):
    """Seeds without config.column_types (known_issues,
    domain_analysis_results, etc.) fall back to read_csv_auto with
    sample_size=-1 — full-file scan inference. At minimum the column
    names from the CSV header must reach DuckDB."""
    project_root = _prepare_project_root(tmp_path)
    seed_csv = project_root / "dbt" / "seeds" / "untyped_test.csv"
    _write_csv(seed_csv, ["id", "title", "priority"], [
        ["1", "First item", "high"],
        ["2", "Second item", "low"],
    ])
    _install_fake_schema_types(monkeypatch, {})  # seed NOT in map

    parent = duckdb.connect(str(project_root / "cpe_analytics.duckdb"),
                            read_only=False)
    try:
        warning = mod.sync_parquet_and_invalidate(
            project_root=project_root,
            seed_name="untyped_test",
            source="test",
            conn=parent,
        )
        assert warning is None

        cols = parent.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'main_seeds' AND table_name = 'untyped_test' "
            "ORDER BY ordinal_position"
        ).fetchall()
        assert [c[0] for c in cols] == ["id", "title", "priority"]

        n = parent.execute(
            'SELECT COUNT(*) FROM main_seeds."untyped_test"'
        ).fetchone()[0]
        assert n == 2
    finally:
        parent.close()


# =========================================================================
# Test 5b — untyped-seed per-column type assertions (Addition 1)
# =========================================================================

def test_untyped_seed_infers_expected_per_column_types(tmp_path, monkeypatch):
    """Regression guard for the untyped-seed inference path. Pre-Option-A,
    dbt seed's sniff was fine for populated columns but weak on all-empty
    ones (#73). Option A uses read_csv_auto with sample_size=-1 (full-file
    scan). Verify that for a known_issues-like schema with fully-populated
    columns, inference picks sensible types (BIGINT for pure-numeric id,
    VARCHAR for text). This would catch a future read_csv behavior
    regression that, e.g., started typing integers as VARCHAR.
    """
    project_root = _prepare_project_root(tmp_path)
    seed_csv = project_root / "dbt" / "seeds" / "ki_like.csv"
    # Mirrors known_issues.csv shape: id (numeric), title/status/priority (varchar).
    _write_csv(seed_csv, ["id", "title", "status", "priority"], [
        ["73", "DAR supersede bug", "resolved", "high"],
        ["74", "Ontology filter bug", "resolved", "high"],
        ["75", "LLM analyzer fresh-table", "resolved", "high"],
    ])
    _install_fake_schema_types(monkeypatch, {})  # untyped path

    parent = duckdb.connect(str(project_root / "cpe_analytics.duckdb"),
                            read_only=False)
    try:
        warning = mod.sync_parquet_and_invalidate(
            project_root=project_root,
            seed_name="ki_like",
            source="test",
            conn=parent,
        )
        assert warning is None

        col_info = dict(parent.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'main_seeds' AND table_name = 'ki_like' "
            "ORDER BY ordinal_position"
        ).fetchall())

        # Numeric id column inferred as an integer family (BIGINT typical).
        assert col_info["id"] in ("BIGINT", "INTEGER"), (
            f"expected integer family for numeric id; got {col_info['id']}"
        )
        # Text columns stay VARCHAR.
        assert col_info["title"] == "VARCHAR"
        assert col_info["status"] == "VARCHAR"
        assert col_info["priority"] == "VARCHAR"
    finally:
        parent.close()
