"""Piece 9 Stage C unit tests for _tar_corpus_loader.

Covers cross-term prior-TAR discovery via s2t_mapping overlap.
Uses an in-memory DuckDB with fabricated main_seeds tables (no real
seed touched).

Coverage:
  - Returns rows from other terms sharing scope tables.
  - Excludes the current term's own rows.
  - Excludes sufficiency rows.
  - Includes superseded rows with superseded_flag=True.
  - Result cap at 20 rows.

Run standalone:
  python tests/test_tar_corpus_loader.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _tar_corpus_loader as loader  # noqa: E402


# ─── fixture helpers ───────────────────────────────────────────────────

def _setup_fixture_conn() -> duckdb.DuckDBPyConnection:
    """Build an in-memory DuckDB with main_seeds schema + fabricated rows."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.business_glossary (
            id VARCHAR, term_name VARCHAR, status VARCHAR,
            scope_derivation_history_json VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.s2t_mapping (
            business_term_id VARCHAR, source_table VARCHAR,
            source_field VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.term_analysis_results (
            id VARCHAR, term_id VARCHAR, row_type VARCHAR,
            analysis_lens VARCHAR, stage VARCHAR, query_index INTEGER,
            query_sql VARCHAR, query_result_json VARCHAR,
            result_row_count INTEGER, interpretation VARCHAR,
            grounded_in_tar_ids VARCHAR, sufficiency_json VARCHAR,
            status VARCHAR, confidence VARCHAR,
            executed_at_utc TIMESTAMP, executed_by VARCHAR,
            superseded_by VARCHAR, run_id VARCHAR,
            llm_usage_json VARCHAR
        )
    """)
    return conn


def _add_term(conn, term_id: str, scope_tables: list[str],
              status: str = "scope_confirmed") -> None:
    conn.execute(
        "INSERT INTO main_seeds.business_glossary VALUES (?, ?, ?, ?)",
        [term_id, f"term_{term_id}", status, "{}"],
    )
    for t in scope_tables:
        conn.execute(
            "INSERT INTO main_seeds.s2t_mapping VALUES (?, ?, ?)",
            [term_id, t, "SOME_FIELD"],
        )


def _add_tar(
    conn, *, tar_id: str, term_id: str, row_type: str = "query",
    analysis_lens: str = "measures_overview",
    stage: str = "framework_floor", query_index: int = 1,
    query_sql: str = "SELECT 1", status: str = "success",
    interpretation: str = "", executed_at_utc: str = "2026-04-22 10:00:00",
    run_id: str = "TARRUN-x",
) -> None:
    conn.execute(
        """
        INSERT INTO main_seeds.term_analysis_results VALUES
        (?, ?, ?, ?, ?, ?, ?, '[]', 0, ?, '[]', '', ?, '', ?, 'test',
         '', ?, '{}')
        """,
        [tar_id, term_id, row_type, analysis_lens, stage, query_index,
         query_sql, interpretation, status, executed_at_utc, run_id],
    )


# ─── tests ─────────────────────────────────────────────────────────────

def test_01_empty_corpus_returns_empty() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg", "equi"])
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert result == []


def test_02_other_term_overlap_returned() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg", "equi"])
    _add_term(conn, "BG-B", ["mseg", "mkpf"])
    _add_tar(conn, tar_id="TAR-00001", term_id="BG-B")
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert len(result) == 1
    assert result[0]["id"] == "TAR-00001"
    assert result[0]["term_id"] == "BG-B"
    assert result[0]["originating_term_name"] == "term_BG-B"


def test_03_excludes_current_term_own_rows() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg"])
    _add_tar(conn, tar_id="TAR-00001", term_id="BG-A")  # self
    _add_term(conn, "BG-B", ["mseg"])
    _add_tar(conn, tar_id="TAR-00002", term_id="BG-B")  # other
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert len(result) == 1
    assert result[0]["id"] == "TAR-00002"


def test_04_excludes_sufficiency_rows() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg"])
    _add_term(conn, "BG-B", ["mseg"])
    _add_tar(
        conn, tar_id="TAR-00001", term_id="BG-B",
        row_type="query",
    )
    _add_tar(
        conn, tar_id="TAR-00002", term_id="BG-B",
        row_type="sufficiency",
        analysis_lens="",
        stage="terminal",
    )
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert len(result) == 1
    assert result[0]["row_type"] == "query"


def test_05_excludes_non_overlapping_scope() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg"])
    _add_term(conn, "BG-B", ["ekpo"])  # no overlap
    _add_tar(conn, tar_id="TAR-00001", term_id="BG-B")
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert result == []


def test_06_superseded_row_included_with_flag() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg"])
    _add_term(conn, "BG-B", ["mseg"])
    _add_tar(
        conn, tar_id="TAR-00001", term_id="BG-B",
        status="superseded",
    )
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert len(result) == 1
    assert result[0]["superseded_flag"] is True


def test_07_current_successor_resolved() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg"])
    _add_term(conn, "BG-B", ["mseg"])
    # Old superseded row
    _add_tar(
        conn, tar_id="TAR-00001", term_id="BG-B",
        analysis_lens="measures_overview", stage="framework_floor",
        status="superseded", executed_at_utc="2026-04-01 10:00:00",
    )
    # New success row on same lens/stage
    _add_tar(
        conn, tar_id="TAR-00050", term_id="BG-B",
        analysis_lens="measures_overview", stage="framework_floor",
        status="success", executed_at_utc="2026-04-22 10:00:00",
    )
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    # Both rows returned (superseded + current success).
    assert len(result) == 2
    superseded = next(r for r in result if r["id"] == "TAR-00001")
    assert superseded["current_successor_id"] == "TAR-00050"


def test_08_result_capped_at_20() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg"])
    _add_term(conn, "BG-B", ["mseg"])
    # 25 rows from BG-B
    for i in range(25):
        _add_tar(
            conn, tar_id=f"TAR-{i:05d}", term_id="BG-B",
            executed_at_utc=f"2026-04-{(i % 28) + 1:02d} 10:00:00",
        )
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert len(result) == 20


def test_09_result_sorted_by_executed_at_desc() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg"])
    _add_term(conn, "BG-B", ["mseg"])
    _add_tar(
        conn, tar_id="TAR-00001", term_id="BG-B",
        executed_at_utc="2026-04-01 10:00:00",
    )
    _add_tar(
        conn, tar_id="TAR-00002", term_id="BG-B",
        executed_at_utc="2026-04-22 10:00:00",
    )
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert result[0]["id"] == "TAR-00002"  # most recent first
    assert result[1]["id"] == "TAR-00001"


def test_10_current_term_no_scope_returns_empty() -> None:
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", [])  # no s2t_mapping rows
    _add_term(conn, "BG-B", ["mseg"])
    _add_tar(conn, tar_id="TAR-00001", term_id="BG-B")
    result = loader.load_candidate_prior_tars(conn, "BG-A")
    assert result == []


# ─── harness ───────────────────────────────────────────────────────────

def test_11_archived_source_term_excluded() -> None:
    """Stage D.1 — archived source terms' TARs must NOT appear as
    citation candidates. Strict archive cascade per decision #67."""
    conn = _setup_fixture_conn()
    _add_term(conn, "BG-A", ["mseg"])
    _add_term(conn, "BG-B-ARCHIVED", ["mseg"], status="archived")
    # Write a TAR for the archived term that would otherwise match.
    _add_tar(conn, tar_id="TAR-00001", term_id="BG-B-ARCHIVED")
    # Also write a TAR for an active term to confirm the non-archived path still works.
    _add_term(conn, "BG-C-ACTIVE", ["mseg"])
    _add_tar(conn, tar_id="TAR-00002", term_id="BG-C-ACTIVE")

    result = loader.load_candidate_prior_tars(conn, "BG-A")

    ids = {r["id"] for r in result}
    assert "TAR-00001" not in ids, (
        "Archived term's TAR leaked through _tar_corpus_loader filter"
    )
    assert "TAR-00002" in ids, "Active term's TAR missing from result"


def _run_standalone() -> int:
    tests = [
        test_01_empty_corpus_returns_empty,
        test_02_other_term_overlap_returned,
        test_03_excludes_current_term_own_rows,
        test_04_excludes_sufficiency_rows,
        test_05_excludes_non_overlapping_scope,
        test_06_superseded_row_included_with_flag,
        test_07_current_successor_resolved,
        test_08_result_capped_at_20,
        test_09_result_sorted_by_executed_at_desc,
        test_10_current_term_no_scope_returns_empty,
        test_11_archived_source_term_excluded,
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
