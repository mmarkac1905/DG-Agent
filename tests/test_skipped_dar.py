"""Stage D.1 — canonical skipped-DAR builder + per-analyzer skip paths.

Tests cover:
  - build_skipped_dar_row emits 18-column shape matching
    domain_analysis_results schema
  - status='skipped' + skip_reason in result_json
  - blockers_addressed always [] for schema uniformity
  - source_tables lowercase normalization
  - run_id sanitization (no ':' or '+' characters)
  - per-analyzer skip detection logic (magnitude heuristic fallback)

Does NOT exercise the full analyzer flow — those are integration/live
tests. Focuses on the shape + decision-logic unit tests.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _skipped_dar as skipped_mod  # noqa: E402


# ─── build_skipped_dar_row shape tests ───────────────────────────────

_DAR_COLUMNS = {
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name", "last_source_ingestion_at",
}


def test_01_builds_18_columns() -> None:
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001",
        analysis_type="date",
        source_tables="mseg",
        skip_reason="no date columns",
        schema_version="abc123",
        last_source_ingestion_at="",
        executed_by="run_date_analysis.py",
    )
    assert set(row.keys()) == _DAR_COLUMNS, (
        f"Column mismatch. Missing: {_DAR_COLUMNS - set(row.keys())}, "
        f"Extra: {set(row.keys()) - _DAR_COLUMNS}"
    )


def test_02_status_is_skipped() -> None:
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="date", source_tables="mseg",
        skip_reason="x", schema_version="x",
        last_source_ingestion_at="", executed_by="x",
    )
    assert row["status"] == "skipped"


def test_03_skip_reason_in_result_json() -> None:
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="code_tables", source_tables="mseg",
        skip_reason="no code column candidates", schema_version="x",
        last_source_ingestion_at="", executed_by="x",
    )
    parsed = json.loads(row["result_json"])
    assert parsed["skip_reason"] == "no code column candidates"


def test_04_blockers_addressed_always_empty_list() -> None:
    """Stage B contract uniformity — every DAR's result_json carries
    blockers_addressed, empty for skipped DARs."""
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="date", source_tables="mseg",
        skip_reason="x", schema_version="x",
        last_source_ingestion_at="", executed_by="x",
    )
    parsed = json.loads(row["result_json"])
    assert parsed["blockers_addressed"] == []


def test_05_source_tables_lowercased() -> None:
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="date", source_tables="MSEG",
        skip_reason="x", schema_version="x",
        last_source_ingestion_at="", executed_by="x",
    )
    assert row["source_tables"] == "mseg"


def test_06_run_id_no_unsafe_chars() -> None:
    """Stage D.1 N3: run_id must not contain ':' or '+' (identifier safety)."""
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="date", source_tables="mseg",
        skip_reason="x", schema_version="x",
        last_source_ingestion_at="", executed_by="x",
    )
    run_id = row["run_id"]
    assert ":" not in run_id
    assert "+" not in run_id


def test_07_run_id_can_be_overridden() -> None:
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="date", source_tables="mseg",
        skip_reason="x", schema_version="x",
        last_source_ingestion_at="", executed_by="x",
        run_id="custom_run_id",
    )
    assert row["run_id"] == "custom_run_id"


def test_08_grain_pair_source_tables_preserved() -> None:
    """Grain pair source_tables = 'sorted,lowercase,comma,joined'."""
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="grain_relationship",
        source_tables="equi,mseg",
        skip_reason="no joinable relationship",
        schema_version="x", last_source_ingestion_at="",
        executed_by="run_grain_relationship_analysis.py",
    )
    assert row["source_tables"] == "equi,mseg"


def test_09_query_sql_is_placeholder_comment() -> None:
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="date", source_tables="mseg",
        skip_reason="no date cols", schema_version="x",
        last_source_ingestion_at="", executed_by="x",
    )
    assert row["query_sql"].startswith("-- skipped:")
    assert "no date cols" in row["query_sql"]


def test_10_row_count_is_zero_string() -> None:
    """row_count column type is INTEGER in schema but CSV DictWriter
    accepts strings. DAR convention uses string '0' / ''."""
    row = skipped_mod.build_skipped_dar_row(
        dar_id="DAR-00001", analysis_type="date", source_tables="mseg",
        skip_reason="x", schema_version="x",
        last_source_ingestion_at="", executed_by="x",
    )
    assert row["row_count"] == "0"


# ─── Magnitude heuristic fallback tests ──────────────────────────────

def _setup_info_schema_fixture() -> duckdb.DuckDBPyConnection:
    """Build an in-memory DuckDB with a raw_sap schema + one table."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.source_column_roles (
            table_name VARCHAR, column_name VARCHAR, role VARCHAR
        )
    """)
    return conn


def test_11_heuristic_finds_decimal_and_integer_columns() -> None:
    conn = _setup_info_schema_fixture()
    conn.execute("""
        CREATE TABLE raw_sap.ekpo (
            EBELN VARCHAR,
            NETWR DECIMAL(13, 2),
            MENGE DECIMAL(13, 3),
            MANDT VARCHAR,
            BEDAT DATE
        )
    """)
    # Reach into the magnitude module's helper.
    import run_magnitude_analysis as mag  # noqa: E402
    result = mag._discover_numeric_columns_heuristic(conn, "ekpo")
    assert "NETWR" in result
    assert "MENGE" in result


def test_12_heuristic_excludes_mandt_client() -> None:
    conn = _setup_info_schema_fixture()
    conn.execute("""
        CREATE TABLE raw_sap.mseg (
            MANDT INTEGER,
            CLIENT INTEGER,
            BWART VARCHAR,
            MENGE DECIMAL(13, 3)
        )
    """)
    import run_magnitude_analysis as mag  # noqa: E402
    result = mag._discover_numeric_columns_heuristic(conn, "mseg")
    assert "MANDT" not in [c.upper() for c in result]
    assert "CLIENT" not in [c.upper() for c in result]
    assert "MENGE" in result


def test_13_heuristic_no_numeric_returns_empty() -> None:
    conn = _setup_info_schema_fixture()
    conn.execute("""
        CREATE TABLE raw_sap.lookup (
            CODE VARCHAR,
            LABEL VARCHAR,
            MANDT INTEGER
        )
    """)
    import run_magnitude_analysis as mag  # noqa: E402
    result = mag._discover_numeric_columns_heuristic(conn, "lookup")
    # MANDT excluded, no other numeric columns → empty.
    assert result == []


def test_14_heuristic_filters_by_raw_sap_schema() -> None:
    """Must not surface columns from other schemas even if table_name collides."""
    conn = _setup_info_schema_fixture()
    conn.execute("CREATE SCHEMA main_marts")
    conn.execute("""
        CREATE TABLE main_marts.overlap_name (
            metric_a DECIMAL(10, 2)
        )
    """)
    # No raw_sap.overlap_name table — should return empty, not metric_a.
    import run_magnitude_analysis as mag  # noqa: E402
    result = mag._discover_numeric_columns_heuristic(conn, "overlap_name")
    assert result == []


# ─── harness ────────────────────────────────────────────────────────

def _run_standalone() -> int:
    tests = [
        test_01_builds_18_columns,
        test_02_status_is_skipped,
        test_03_skip_reason_in_result_json,
        test_04_blockers_addressed_always_empty_list,
        test_05_source_tables_lowercased,
        test_06_run_id_no_unsafe_chars,
        test_07_run_id_can_be_overridden,
        test_08_grain_pair_source_tables_preserved,
        test_09_query_sql_is_placeholder_comment,
        test_10_row_count_is_zero_string,
        test_11_heuristic_finds_decimal_and_integer_columns,
        test_12_heuristic_excludes_mandt_client,
        test_13_heuristic_no_numeric_returns_empty,
        test_14_heuristic_filters_by_raw_sap_schema,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  [PASS] {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  [FAIL] {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERR ] {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_standalone())
