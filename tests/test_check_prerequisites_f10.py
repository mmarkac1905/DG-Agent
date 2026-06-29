"""F10 (#87) — check_prerequisites multi-table source_tables parse fix.

Pre-fix: `LOWER(source_tables) = LOWER(?)` required exact string equality,
so a DAR with `source_tables='ekbe,ekpo'` (multi-table grain_relationship
or join_cardinality output) did not count toward either constituent
table's coverage. ~9% of current DARs are multi-table; falsely flagged
as "needs domain EDA" when only multi-table coverage existed.

Post-fix: query splits source_tables on comma + trims each element +
tests list membership. Multi-table DARs count toward each constituent
table; substring matches (e.g. 'ek' against 'eket') are excluded.

Tests use in-memory DuckDB with a minimal business_glossary /
s2t_mapping / domain_analysis_results fixture matching the columns
read by _load_term + check_prerequisites.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _scope_derivation as sd  # noqa: E402


def _setup_conn() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA main_seeds")
    # business_glossary: minimal columns required by _load_term
    conn.execute("""
        CREATE TABLE main_seeds.business_glossary (
            id VARCHAR, term_name VARCHAR, display_name VARCHAR,
            definition VARCHAR, unit VARCHAR, grain VARCHAR,
            domain VARCHAR, notes VARCHAR,
            business_join_description VARCHAR,
            business_filter_description VARCHAR,
            status VARCHAR,
            scope_derivation_history_json VARCHAR
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


def _add_term(conn, term_id: str, status: str,
              scope_tables: list[str]) -> None:
    conn.execute(
        "INSERT INTO main_seeds.business_glossary "
        "(id, term_name, status) VALUES (?, ?, ?)",
        [term_id, term_id, status],
    )
    for t in scope_tables:
        conn.execute(
            "INSERT INTO main_seeds.s2t_mapping VALUES (?, ?)",
            [term_id, t],
        )


def _add_dar(conn, dar_id: str, source_tables: str,
             analysis_type: str = "completeness",
             status: str = "success") -> None:
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results "
        "(id, source_tables, analysis_type, status) VALUES (?, ?, ?, ?)",
        [dar_id, source_tables, analysis_type, status],
    )


# ─── Tests ─────────────────────────────────────────────────────────────

def test_multi_table_dar_counts_toward_both_constituent_tables() -> None:
    """F10 primary: 'ekbe,ekpo' DAR satisfies both ekbe and ekpo prereq.

    Pre-fix: neither table sees the DAR (exact-string equality fails on
    the comma-joined value). Post-fix: both count.
    """
    conn = _setup_conn()
    _add_term(conn, "BG-F10", "scope_confirmed", ["ekbe", "ekpo"])
    # Only coverage source: a single multi-table DAR.
    _add_dar(conn, "DAR-001", "ekbe,ekpo", "grain_relationship")

    result = sd.check_prerequisites("BG-F10", conn=conn)

    assert result.domain_eda_status == {"ekbe": True, "ekpo": True}, (
        f"expected both ekbe and ekpo to count multi-table DAR; "
        f"got {result.domain_eda_status}"
    )
    assert result.domain_eda_needed_on == [], (
        f"expected no tables needing EDA; got {result.domain_eda_needed_on}"
    )


def test_multi_table_dar_with_whitespace_handled() -> None:
    """F10 robustness: 'ekbe, ekpo' (space after comma) still parses.

    No current writer emits this format, but the spec called the
    whitespace gap out as a risk in the LIKE alternative. string_split
    + trim handles it; this test pins that behavior.
    """
    conn = _setup_conn()
    _add_term(conn, "BG-WS", "scope_confirmed", ["ekbe", "ekpo"])
    _add_dar(conn, "DAR-WS", "ekbe, ekpo", "grain_relationship")

    result = sd.check_prerequisites("BG-WS", conn=conn)

    assert result.domain_eda_status == {"ekbe": True, "ekpo": True}


def test_substring_does_not_falsely_match() -> None:
    """F10 negative: a DAR for 'eket' must not satisfy a request for 'ek'.

    Pure-LIKE alternatives (e.g. `source_tables LIKE '%ek%'`) would
    false-positive here. string_split + trim + list_contains is exact
    on token boundaries.
    """
    conn = _setup_conn()
    _add_term(conn, "BG-SUB", "scope_confirmed", ["ek"])
    _add_dar(conn, "DAR-SUB-1", "eket", "completeness")
    _add_dar(conn, "DAR-SUB-2", "eket,ekpo", "grain_relationship")

    result = sd.check_prerequisites("BG-SUB", conn=conn)

    assert result.domain_eda_status == {"ek": False}, (
        f"'ek' must not match 'eket' or 'eket,ekpo'; "
        f"got {result.domain_eda_status}"
    )
    assert result.domain_eda_needed_on == ["ek"]


def test_single_table_dar_still_counts_after_fix() -> None:
    """Regression guard: single-table DARs (the dominant case, 91% of
    current data) continue to satisfy prereq after the rewrite.
    """
    conn = _setup_conn()
    _add_term(conn, "BG-REG", "scope_confirmed", ["mseg"])
    _add_dar(conn, "DAR-REG-1", "mseg", "completeness")

    result = sd.check_prerequisites("BG-REG", conn=conn)

    assert result.domain_eda_status == {"mseg": True}


def test_failed_dars_do_not_count() -> None:
    """Regression guard: status='failed' DARs do not satisfy prereq,
    even when they reference the table — `status='success'` filter
    must remain in effect after the parse-rewrite.
    """
    conn = _setup_conn()
    _add_term(conn, "BG-FAIL", "scope_confirmed", ["mseg"])
    _add_dar(conn, "DAR-FAIL-1", "mseg", "completeness", status="failed")
    _add_dar(conn, "DAR-FAIL-2", "ekbe,mseg", "grain_relationship",
             status="failed")

    result = sd.check_prerequisites("BG-FAIL", conn=conn)

    assert result.domain_eda_status == {"mseg": False}


# ─── harness ───────────────────────────────────────────────────────────

def _run_standalone() -> int:
    tests = [
        test_multi_table_dar_counts_toward_both_constituent_tables,
        test_multi_table_dar_with_whitespace_handled,
        test_substring_does_not_falsely_match,
        test_single_table_dar_still_counts_after_fix,
        test_failed_dars_do_not_count,
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
