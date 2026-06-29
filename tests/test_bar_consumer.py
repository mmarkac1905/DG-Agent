"""Tests for scripts/_bar_consumer.py.

Currently scoped to the C6 `resolve_latest_bar` resolver. The existing
`resolve_promoted_bar` is exercised by the Piece-8.5 integration tests
and the F.4 retry tests; not duplicated here.

Pattern: in-memory DuckDB with a minimal `main_seeds.business_term_analysis_results`
table, just the columns the resolver reads.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from _bar_consumer import (  # noqa: E402
    BarConsumptionError,
    resolve_latest_bar,
)


_BAR_TABLE_DDL = """
CREATE SCHEMA IF NOT EXISTS main_seeds;
CREATE TABLE main_seeds.business_term_analysis_results (
    id VARCHAR,
    business_term_id VARCHAR,
    status VARCHAR,
    convergence_reason VARCHAR,
    sourcing_recommendations VARCHAR,
    iteration_trace VARCHAR,
    bridge_coverage_consulted VARCHAR,
    finished_at_utc TIMESTAMP,
    superseded_by VARCHAR
);
"""


@pytest.fixture()
def conn():
    c = duckdb.connect(":memory:")
    c.execute(_BAR_TABLE_DDL)
    yield c
    c.close()


def _insert(c, **kwargs):
    """Insert a BAR row with sensible defaults — only override what
    the test cares about."""
    defaults = {
        "id": "BAR-00001",
        "business_term_id": "BG999",
        "status": "converged",
        "convergence_reason": "converged_soft",
        "sourcing_recommendations": None,
        "iteration_trace": "[]",
        "bridge_coverage_consulted": None,
        "finished_at_utc": "2026-04-25 10:00:00",
        "superseded_by": None,
    }
    defaults.update(kwargs)
    c.execute(
        """
        INSERT INTO main_seeds.business_term_analysis_results VALUES
        (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [defaults[k] for k in (
            "id", "business_term_id", "status", "convergence_reason",
            "sourcing_recommendations", "iteration_trace",
            "bridge_coverage_consulted", "finished_at_utc", "superseded_by",
        )],
    )


def test_resolve_latest_bar_no_rows_returns_none(conn):
    assert resolve_latest_bar(conn, "BG999") is None


def test_resolve_latest_bar_returns_most_recent_finished(conn):
    _insert(conn, id="BAR-00001", finished_at_utc="2026-04-25 10:00:00",
            status="converged")
    _insert(conn, id="BAR-00002", finished_at_utc="2026-04-26 10:00:00",
            status="needs_data_extension",
            convergence_reason="hard_stop_bridge_unreachable")
    _insert(conn, id="BAR-00003", finished_at_utc="2026-04-24 10:00:00",
            status="hard_stop")

    result = resolve_latest_bar(conn, "BG999")
    assert result is not None
    assert result["id"] == "BAR-00002"
    assert result["status"] == "needs_data_extension"
    assert result["convergence_reason"] == "hard_stop_bridge_unreachable"


def test_resolve_latest_bar_excludes_in_progress(conn):
    # In-progress: finished_at_utc IS NULL
    _insert(conn, id="BAR-00099", finished_at_utc=None,
            status="in_progress")
    # Older but finished
    _insert(conn, id="BAR-00010", finished_at_utc="2026-04-20 10:00:00",
            status="converged")

    result = resolve_latest_bar(conn, "BG999")
    assert result is not None
    assert result["id"] == "BAR-00010"


def test_resolve_latest_bar_excludes_superseded(conn):
    _insert(conn, id="BAR-00050", finished_at_utc="2026-04-26 10:00:00",
            status="hard_stop", superseded_by="BAR-00051")
    _insert(conn, id="BAR-00040", finished_at_utc="2026-04-25 10:00:00",
            status="converged")

    result = resolve_latest_bar(conn, "BG999")
    assert result is not None
    # Most recent NON-superseded row wins
    assert result["id"] == "BAR-00040"


def test_resolve_latest_bar_malformed_json_raises(conn):
    _insert(conn, id="BAR-00077", finished_at_utc="2026-04-26 10:00:00",
            iteration_trace="not-valid-json{")

    with pytest.raises(BarConsumptionError, match="iteration_trace"):
        resolve_latest_bar(conn, "BG999")


def test_resolve_latest_bar_parses_sourcing_recommendations(conn):
    sr = {"summary": {"total_recommendations": 5},
          "validated_recommendations": [{"table_name": "ekko"}]}
    _insert(conn, id="BAR-00100", finished_at_utc="2026-04-28 10:00:00",
            status="needs_data_extension",
            sourcing_recommendations=json.dumps(sr))

    result = resolve_latest_bar(conn, "BG999")
    assert result is not None
    assert result["sourcing_recommendations"] == sr


def test_resolve_latest_bar_other_term_id_filtered_out(conn):
    _insert(conn, id="BAR-00200", business_term_id="BG_OTHER",
            finished_at_utc="2026-04-29 10:00:00")
    _insert(conn, id="BAR-00201", business_term_id="BG999",
            finished_at_utc="2026-04-26 10:00:00",
            status="converged")

    result = resolve_latest_bar(conn, "BG999")
    assert result is not None
    assert result["id"] == "BAR-00201"
