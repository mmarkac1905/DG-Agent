"""KI-114 — Unified blocker_state view tests.

The view at dbt/models/knowledge/knowledge_blocker_state.sql reconciles
three persistent representations of a Stage A blocker:

  A (filed):   business_glossary.scope_derivation_history_json
  B (claimed): domain_analysis_results.result_json.blockers_addressed
  C (closed):  term_analysis_results.sufficiency_json.blockers_resolution

Tests build an in-memory DuckDB with the relevant seed schemas,
materialize the view from the .sql file, and assert that
current_status correctly reflects the mapping:

  resolution_status='resolved' or 'not_applicable'  → 'RESOLVED'
  resolution_status='could_not_resolve'             → 'PENDING'
  resolution_status='escalated_to_analyst'          → 'PENDING'
  resolution_status IS NULL (no Stage C TAR yet)    → 'PENDING'
  resolution_status outside enum                     → 'AMBIGUOUS'
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
_VIEW_SQL = _ROOT / "dbt" / "models" / "knowledge" / "knowledge_blocker_state.sql"


def _build_db() -> duckdb.DuckDBPyConnection:
    """Create an in-memory DuckDB with the seed schemas + view."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("CREATE SCHEMA main_knowledge")
    conn.execute(
        """
        CREATE TABLE main_seeds.business_glossary (
            id VARCHAR, term_name VARCHAR, status VARCHAR,
            scope_derivation_history_json VARCHAR
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE main_seeds.term_analysis_results (
            id VARCHAR, term_id VARCHAR, row_type VARCHAR,
            sufficiency_json VARCHAR, status VARCHAR,
            executed_at_utc TIMESTAMP
        )
        """
    )
    sql = _VIEW_SQL.read_text(encoding="utf-8")
    # Strip dbt jinja config line (in-memory test runs raw SQL).
    sql_clean = "\n".join(
        line for line in sql.splitlines() if not line.startswith("{{ config")
    )
    conn.execute(
        "CREATE OR REPLACE VIEW main_knowledge.knowledge_blocker_state AS "
        + sql_clean
    )
    return conn


def _insert_term(
    conn: duckdb.DuckDBPyConnection,
    *,
    term_id: str,
    term_name: str = "test term",
    status: str = "ready_for_s2t",
    blockers: list[dict] | None = None,
) -> None:
    """Insert one business_glossary row with a confirmed iteration carrying
    the given blockers list."""
    history = {
        "iterations": [
            {
                "iter_num": 1,
                "analyst_action": "confirmed",
                "llm_response": {"blockers": blockers or []},
            }
        ],
        "final_iter_num": 1,
        "confirmed_at_utc": "2026-04-29T10:00:00Z",
    }
    conn.execute(
        "INSERT INTO main_seeds.business_glossary "
        "(id, term_name, status, scope_derivation_history_json) "
        "VALUES (?, ?, ?, ?)",
        [term_id, term_name, status, json.dumps(history)],
    )


def _insert_sufficiency(
    conn: duckdb.DuckDBPyConnection,
    *,
    term_id: str,
    blockers_resolution: list[dict],
    tar_id: str = "TAR-00001",
    executed_at: str = "2026-04-29 12:00:00",
) -> None:
    """Insert one term_analysis_results sufficiency row carrying the
    given blockers_resolution list."""
    suff = {
        "declared_sufficient": True,
        "blockers_resolution": blockers_resolution,
    }
    conn.execute(
        "INSERT INTO main_seeds.term_analysis_results "
        "(id, term_id, row_type, sufficiency_json, status, executed_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [tar_id, term_id, "sufficiency", json.dumps(suff), "success",
         executed_at],
    )


def _query_status(
    conn: duckdb.DuckDBPyConnection, term_id: str, short_title: str,
) -> str | None:
    row = conn.execute(
        """
        SELECT current_status FROM main_knowledge.knowledge_blocker_state
        WHERE term_id = ? AND blocker_short_title = ?
        """,
        [term_id, short_title],
    ).fetchone()
    return row[0] if row else None


def test_view_pending_blocker_returns_pending_status() -> None:
    """Stage A files a blocker; no Stage C TAR yet → current_status='PENDING'."""
    conn = _build_db()
    try:
        _insert_term(conn, term_id="BG-X", blockers=[{
            "short_title": "BWART unclear",
            "type": "scope_concern",
            "resolves_in": "domain_eda",
            "tables": ["mseg"],
        }])
        # No sufficiency row — no resolution.
        assert _query_status(conn, "BG-X", "BWART unclear") == "PENDING"
    finally:
        conn.close()


def test_view_resolved_blocker_returns_resolved_status() -> None:
    """Stage C marks the blocker status='resolved' → 'RESOLVED'."""
    conn = _build_db()
    try:
        _insert_term(conn, term_id="BG-X", blockers=[{
            "short_title": "BWART unclear",
            "type": "scope_concern",
            "resolves_in": "domain_eda",
            "tables": ["mseg"],
        }])
        _insert_sufficiency(conn, term_id="BG-X", blockers_resolution=[{
            "blocker_short_title": "BWART unclear",
            "status": "resolved",
            "evidence": "DAR-00500 covers it.",
        }])
        assert _query_status(conn, "BG-X", "BWART unclear") == "RESOLVED"
    finally:
        conn.close()


def test_view_not_applicable_blocker_returns_resolved_status() -> None:
    """Stage C marks status='not_applicable' (the BG029 BWART case) →
    'RESOLVED' (analyst no longer needs to address)."""
    conn = _build_db()
    try:
        _insert_term(conn, term_id="BG-Y", blockers=[{
            "short_title": "BWART filter unclear",
            "type": "scope_concern",
            "resolves_in": "domain_eda",
            "tables": ["mseg"],
        }])
        _insert_sufficiency(conn, term_id="BG-Y", blockers_resolution=[{
            "blocker_short_title": "BWART filter unclear",
            "status": "not_applicable",
            "evidence": "DAR already resolved this.",
        }])
        assert _query_status(conn, "BG-Y", "BWART filter unclear") == "RESOLVED"
    finally:
        conn.close()


def test_view_could_not_resolve_returns_pending() -> None:
    """Stage C status='could_not_resolve' → still 'PENDING' (analyst still
    needs to provide more inputs)."""
    conn = _build_db()
    try:
        _insert_term(conn, term_id="BG-Z", blockers=[{
            "short_title": "Need vendor mapping",
            "type": "missing_domain_eda",
            "resolves_in": "domain_eda",
            "tables": ["lfa1"],
        }])
        _insert_sufficiency(conn, term_id="BG-Z", blockers_resolution=[{
            "blocker_short_title": "Need vendor mapping",
            "status": "could_not_resolve",
            "evidence": "no mapping table available",
        }])
        assert _query_status(conn, "BG-Z", "Need vendor mapping") == "PENDING"
    finally:
        conn.close()


def test_view_no_resolution_record_returns_pending() -> None:
    """Term has Stage C sufficiency but blockers_resolution doesn't include
    this blocker (LLM omitted it) → 'PENDING'. Defensive: malformed Stage C
    output shouldn't silently mark blockers resolved."""
    conn = _build_db()
    try:
        _insert_term(conn, term_id="BG-W", blockers=[
            {
                "short_title": "Blocker A",
                "type": "scope_concern",
                "resolves_in": "domain_eda",
                "tables": ["mseg"],
            },
            {
                "short_title": "Blocker B",
                "type": "scope_concern",
                "resolves_in": "domain_eda",
                "tables": ["mseg"],
            },
        ])
        _insert_sufficiency(conn, term_id="BG-W", blockers_resolution=[{
            # Only resolves Blocker A; Blocker B omitted.
            "blocker_short_title": "Blocker A",
            "status": "resolved",
            "evidence": "...",
        }])
        assert _query_status(conn, "BG-W", "Blocker A") == "RESOLVED"
        assert _query_status(conn, "BG-W", "Blocker B") == "PENDING"
    finally:
        conn.close()


def test_view_excludes_archived_terms() -> None:
    """Term in status='archived' should not contribute blockers (archived
    statuses are outside the eligible set)."""
    conn = _build_db()
    try:
        _insert_term(conn, term_id="BG-ARC", status="archived", blockers=[{
            "short_title": "Should not appear",
            "type": "scope_concern",
            "resolves_in": "domain_eda",
            "tables": ["mseg"],
        }])
        rows = conn.execute(
            "SELECT COUNT(*) FROM main_knowledge.knowledge_blocker_state "
            "WHERE term_id = 'BG-ARC'"
        ).fetchone()
        assert rows[0] == 0
    finally:
        conn.close()


def test_view_handles_unknown_resolution_status_as_ambiguous() -> None:
    """If Stage C emits a resolution_status outside the documented enum,
    current_status='AMBIGUOUS' — surfacing the unexpected state for
    analyst review rather than silently defaulting either way."""
    conn = _build_db()
    try:
        _insert_term(conn, term_id="BG-AMB", blockers=[{
            "short_title": "Some blocker",
            "type": "scope_concern",
            "resolves_in": "domain_eda",
            "tables": ["mseg"],
        }])
        _insert_sufficiency(conn, term_id="BG-AMB", blockers_resolution=[{
            "blocker_short_title": "Some blocker",
            "status": "weird_unexpected_value",
            "evidence": "...",
        }])
        assert _query_status(conn, "BG-AMB", "Some blocker") == "AMBIGUOUS"
    finally:
        conn.close()


def test_view_uses_latest_sufficiency_per_term() -> None:
    """When a term has multiple sufficiency rows (re-runs), the view
    should use the most recent one (matches the runner's supersession
    semantics + UI's latest-wins display)."""
    conn = _build_db()
    try:
        _insert_term(conn, term_id="BG-MULTI", blockers=[{
            "short_title": "Blocker M",
            "type": "scope_concern",
            "resolves_in": "domain_eda",
            "tables": ["mseg"],
        }])
        # Older run: resolved.
        _insert_sufficiency(
            conn, term_id="BG-MULTI", tar_id="TAR-OLD",
            executed_at="2026-04-25 10:00:00",
            blockers_resolution=[{
                "blocker_short_title": "Blocker M",
                "status": "resolved",
                "evidence": "older",
            }],
        )
        # Newer run: could_not_resolve. Latest wins → PENDING.
        _insert_sufficiency(
            conn, term_id="BG-MULTI", tar_id="TAR-NEW",
            executed_at="2026-04-29 14:00:00",
            blockers_resolution=[{
                "blocker_short_title": "Blocker M",
                "status": "could_not_resolve",
                "evidence": "newer overrides",
            }],
        )
        assert _query_status(conn, "BG-MULTI", "Blocker M") == "PENDING"
    finally:
        conn.close()
