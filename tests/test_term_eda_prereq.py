"""Stage D.1 unit tests for _term_eda_prereq.

Covers all prereq branches:
  - term not found / status invalid / scope empty
  - all 8 analyzers present → ready
  - missing specific analyzer(s) → not ready with populated
    missing_analyzers_per_table
  - grain_relationship pair coverage (single-table auto; multi-table pair
    DARs; partial pair coverage)
  - status='skipped' DARs satisfy prereq alongside 'success'
  - performance_baseline auto-satisfied by magnitude DAR
  - term-agnostic filter (any-context DAR satisfies)

Uses in-memory DuckDB with the extended fixture (includes analysis_type).
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _term_eda_prereq as prereq  # noqa: E402


ALL_PER_TABLE = [
    # known_issue #77: fixture labels align with production DAR storage
    # labels (run_date_analysis.py writes 'temporal_coverage',
    # run_segmentation_analysis.py writes 'segmentation_threshold').
    # Prior fixture used 'date'/'segmentation' which matched the buggy
    # prereq code path but never exercised real DAR labels.
    "completeness", "dimensions", "magnitude", "code_tables",
    "temporal_coverage", "segmentation_threshold",
]


def _setup_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.business_glossary (
            id VARCHAR, status VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.s2t_mapping (
            business_term_id VARCHAR, source_table VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, source_tables VARCHAR,
            analysis_type VARCHAR, status VARCHAR
        )
    """)
    return conn


def _add_term(conn, term_id: str, status: str, scope_tables: list[str]) -> None:
    conn.execute(
        "INSERT INTO main_seeds.business_glossary VALUES (?, ?)",
        [term_id, status],
    )
    for t in scope_tables:
        conn.execute(
            "INSERT INTO main_seeds.s2t_mapping VALUES (?, ?)",
            [term_id, t],
        )


def _add_dar(conn, dar_id: str, source_tables: str,
             analysis_type: str, status: str = "success") -> None:
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results "
        "VALUES (?, ?, ?, ?)",
        [dar_id, source_tables, analysis_type, status],
    )


def _add_all_per_table_dars(conn, table: str,
                            skip_types: set[str] | None = None,
                            skipped_as_skipped: set[str] | None = None) -> None:
    """Add one success DAR per per-table analyzer.

    skip_types: analyzer types to omit entirely (simulate missing coverage).
    skipped_as_skipped: analyzer types to emit as status='skipped'
        instead of 'success' (should still satisfy prereq).
    """
    skip_types = skip_types or set()
    skipped_as_skipped = skipped_as_skipped or set()
    for atype in ALL_PER_TABLE:
        if atype in skip_types:
            continue
        status = "skipped" if atype in skipped_as_skipped else "success"
        _add_dar(conn, f"DAR-{table}-{atype}", table, atype, status=status)


# ─── Tests ─────────────────────────────────────────────────────────────

def test_01_term_not_found() -> None:
    conn = _setup_conn()
    result = prereq.check_term_eda_prereq(conn, "BG-MISSING")
    assert result["ready"] is False
    assert result["reason"] == "term_not_found"


def test_02_term_status_invalid_draft() -> None:
    conn = _setup_conn()
    _add_term(conn, "BG-A", "draft", [])
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is False
    assert result["reason"] == "term_status_invalid"


def test_03_term_status_invalid_archived() -> None:
    conn = _setup_conn()
    _add_term(conn, "BG-A", "archived", [])
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is False
    assert result["reason"] == "term_status_invalid"


def test_04_scope_empty() -> None:
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", [])
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is False
    assert result["reason"] == "scope_empty"
    assert result["scope_tables"] == []


def test_05_all_8_analyzers_present_single_table() -> None:
    """Single-table scope: all 6 per-table analyzers + grain auto-satisfied → ready."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["mseg"])
    _add_all_per_table_dars(conn, "mseg")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True
    assert result["reason"] == "ready"
    assert result["scope_tables"] == ["mseg"]
    assert result["missing_analyzers_per_table"] == {}
    assert result["missing_grain_pairs"] == []


def test_06_missing_code_tables_not_ready() -> None:
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["mseg"])
    _add_all_per_table_dars(conn, "mseg", skip_types={"code_tables"})
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is False
    assert result["reason"] == "analyzer_coverage_incomplete"
    assert "mseg" in result["missing_analyzers_per_table"]
    assert "code_tables" in result["missing_analyzers_per_table"]["mseg"]


def test_07_skipped_dar_satisfies_prereq() -> None:
    """Per Stage D.1: status='skipped' DARs count as satisfying."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["mseg"])
    # Date + segmentation emit as 'skipped' (table has no date/numeric cols);
    # other 4 are 'success'.
    _add_all_per_table_dars(
        conn, "mseg",
        skipped_as_skipped={"temporal_coverage", "segmentation_threshold"},
    )
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True, (
        f"skipped DARs should satisfy prereq; got {result}"
    )


def test_08_magnitude_missing_means_performance_baseline_also_flagged() -> None:
    """performance_baseline is auto-satisfied by magnitude. When magnitude
    is missing, the UI grid shows BOTH flagged as missing so the analyst
    knows one fix addresses both."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["mseg"])
    _add_all_per_table_dars(conn, "mseg", skip_types={"magnitude"})
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is False
    missing = result["missing_analyzers_per_table"]["mseg"]
    assert "magnitude" in missing
    assert "performance_baseline" in missing


def test_09_magnitude_present_satisfies_performance_baseline() -> None:
    """When magnitude is present, performance_baseline is not flagged."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["mseg"])
    _add_all_per_table_dars(conn, "mseg")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True
    missing_of_table = result["missing_analyzers_per_table"].get("mseg", [])
    assert "performance_baseline" not in missing_of_table


def test_10_grain_pair_satisfies_both_tables() -> None:
    """2-table scope: grain pair DAR on 'mseg,equi' satisfies both tables."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["equi", "mseg"])
    _add_all_per_table_dars(conn, "mseg")
    _add_all_per_table_dars(conn, "equi")
    # Pair DAR in sorted order — prereq expects 'equi,mseg'.
    _add_dar(conn, "DAR-GR-1", "equi,mseg", "grain_relationship")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True, f"got {result}"
    assert result["missing_grain_pairs"] == []


def test_11_3table_scope_partial_grain_coverage() -> None:
    """3-table scope, only 1 of 3 pairs has grain DAR → remaining 2 pairs missing."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["equi", "mseg", "ekpo"])
    for t in ("equi", "mseg", "ekpo"):
        _add_all_per_table_dars(conn, t)
    # Only one pair DAR; leaves equi+ekpo and ekpo+mseg missing.
    # Sorted pair format: "equi,mseg"
    _add_dar(conn, "DAR-GR-1", "equi,mseg", "grain_relationship")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is False
    missing_pairs = set(result["missing_grain_pairs"])
    assert ("ekpo", "equi") in missing_pairs
    assert ("ekpo", "mseg") in missing_pairs
    assert ("equi", "mseg") not in missing_pairs


def test_12_3table_scope_all_pairs_covered_mixed_statuses() -> None:
    """3-table scope, all 3 pairs have DARs (some skipped) → ready."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["equi", "mseg", "ekpo"])
    for t in ("equi", "mseg", "ekpo"):
        _add_all_per_table_dars(conn, t)
    _add_dar(conn, "DAR-GR-1", "equi,mseg", "grain_relationship", status="success")
    _add_dar(conn, "DAR-GR-2", "ekpo,equi", "grain_relationship", status="skipped")
    _add_dar(conn, "DAR-GR-3", "ekpo,mseg", "grain_relationship", status="success")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True, f"got {result}"


def test_13_term_eda_pending_is_valid_start_state() -> None:
    conn = _setup_conn()
    _add_term(conn, "BG-A", "term_eda_pending", ["mseg"])
    _add_all_per_table_dars(conn, "mseg")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True


def test_14_ready_for_s2t_is_valid_re_run_state() -> None:
    conn = _setup_conn()
    _add_term(conn, "BG-A", "ready_for_s2t", ["mseg"])
    _add_all_per_table_dars(conn, "mseg")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True


def test_15_error_status_dars_dont_satisfy_prereq() -> None:
    """status='error' does NOT satisfy — only success + skipped."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["mseg"])
    # All analyzers have error DARs (not success / skipped). Insert with
    # storage labels (ALL_PER_TABLE) — what analyzers actually write.
    for atype in ALL_PER_TABLE:
        _add_dar(conn, f"DAR-err-{atype}", "mseg", atype, status="error")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is False
    assert result["reason"] == "analyzer_coverage_incomplete"
    # Output uses UI labels ('date', 'segmentation') — #77 fix preserves
    # user-facing output format while translating for SQL internally.
    missing = set(result["missing_analyzers_per_table"]["mseg"])
    _ui_labels = {"completeness", "dimensions", "magnitude", "code_tables",
                  "date", "segmentation"}
    for label in _ui_labels:
        assert label in missing, f"expected {label!r} in missing; got {missing}"


def test_16_scope_tables_field_always_populated() -> None:
    """Stage D.1 — scope_tables list must be present on response even
    when partial/empty coverage, so UI can render full grid."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["equi", "mseg"])
    # No DARs yet.
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["scope_tables"] == ["equi", "mseg"]
    assert result["ready"] is False


def test_17_next_steps_bounded_by_scope_count_plus_one() -> None:
    """Bounded: one entry per table with missing analyzers, plus 1 for grain pairs."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["equi", "mseg", "ekpo"])
    # No DARs at all → every table has 6 missing analyzers + all 3 pairs missing.
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    steps = result["next_steps"]
    # 3 tables × 1 step each + 1 grain-pairs step = 4 total.
    assert len(steps) == 4, f"expected 4 steps, got {len(steps)}: {steps}"


def test_18_term_agnostic_dar_filter() -> None:
    """Any-context success DAR satisfies prereq."""
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["mseg"])
    _add_all_per_table_dars(conn, "mseg")
    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True


def test_prereq_recognizes_temporal_coverage_and_segmentation_threshold_under_their_real_labels() -> None:
    """Regression for known_issue #77.

    Prior to the fix, _term_eda_prereq._PER_TABLE_ANALYZERS ran SQL
    with WHERE analysis_type='date' and 'segmentation' — but
    run_date_analysis.py writes 'temporal_coverage' and
    run_segmentation_analysis.py writes 'segmentation_threshold'. The
    query returned zero rows for those two analyzers on every scope
    table, so production terms with full DAR coverage were reported as
    missing those analyzers and Stage C was unreachable.

    Fixture inserts DARs with the REAL production storage labels; the
    prereq's _DAR_STORAGE_LABEL translation must find them and report
    ready=True. Previously-passing tests used 'date'/'segmentation'
    labels that matched the buggy code but never exercised production
    writes.
    """
    conn = _setup_conn()
    _add_term(conn, "BG-A", "scope_confirmed", ["mseg"])
    # Use exact production DAR storage labels — no translation on insert.
    for atype in ("completeness", "dimensions", "magnitude", "code_tables",
                  "temporal_coverage", "segmentation_threshold"):
        _add_dar(conn, f"DAR-prod-{atype}", "mseg", atype, status="success")

    result = prereq.check_term_eda_prereq(conn, "BG-A")
    assert result["ready"] is True, (
        f"prereq must recognize DARs written with production storage "
        f"labels (temporal_coverage, segmentation_threshold) — got {result}"
    )
    assert result["missing_analyzers_per_table"] == {}


# ─── harness ───────────────────────────────────────────────────────────

def _run_standalone() -> int:
    tests = [
        test_01_term_not_found,
        test_02_term_status_invalid_draft,
        test_03_term_status_invalid_archived,
        test_04_scope_empty,
        test_05_all_8_analyzers_present_single_table,
        test_06_missing_code_tables_not_ready,
        test_07_skipped_dar_satisfies_prereq,
        test_08_magnitude_missing_means_performance_baseline_also_flagged,
        test_09_magnitude_present_satisfies_performance_baseline,
        test_10_grain_pair_satisfies_both_tables,
        test_11_3table_scope_partial_grain_coverage,
        test_12_3table_scope_all_pairs_covered_mixed_statuses,
        test_13_term_eda_pending_is_valid_start_state,
        test_14_ready_for_s2t_is_valid_re_run_state,
        test_15_error_status_dars_dont_satisfy_prereq,
        test_16_scope_tables_field_always_populated,
        test_17_next_steps_bounded_by_scope_count_plus_one,
        test_18_term_agnostic_dar_filter,
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
