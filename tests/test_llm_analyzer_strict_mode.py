"""Tests for known_issue #75 — LLM analyzers must pass strict=False on
fresh tables (zero prior DARs) to avoid ContextDegradedError crash.

Covers:
  - has_prior_dars_for_scope helper (empty vs populated).
  - Each of the 4 LLM analyzers passes strict=False to assemble_context
    when the helper reports no priors.

The analyzer tests monkeypatch `assemble_context` and
`has_prior_dars_for_scope` to capture the strict argument without
triggering the actual LLM + SQL flow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _context_assembler as ca  # noqa: E402


# =========================================================================
# helper — has_prior_dars_for_scope
# =========================================================================

def _seed_dar_table(conn, rows: list[dict]) -> None:
    """Create a minimal main_seeds.domain_analysis_results table and
    insert the given rows."""
    conn.execute("CREATE SCHEMA IF NOT EXISTS main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR,
            analysis_type VARCHAR,
            source_tables VARCHAR,
            superseded_by VARCHAR,
            status VARCHAR
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO main_seeds.domain_analysis_results "
            "VALUES (?, ?, ?, ?, ?)",
            [r.get("id"), r.get("analysis_type"), r.get("source_tables"),
             r.get("superseded_by", ""), r.get("status", "success")],
        )


def test_helper_returns_false_when_scope_has_no_priors(tmp_path, monkeypatch):
    """Fresh table: helper reports no priors → analyzer should pass
    strict=False."""
    db = tmp_path / "probe.duckdb"
    conn = duckdb.connect(str(db))
    _seed_dar_table(conn, [])  # empty seed
    conn.close()

    monkeypatch.setattr(ca, "DB_PATH", db)
    assert ca.has_prior_dars_for_scope(["objk"]) is False


def test_helper_returns_true_when_scope_has_current_priors(tmp_path, monkeypatch):
    """Prior DARs exist → strict=True preserved (default behavior)."""
    db = tmp_path / "probe.duckdb"
    conn = duckdb.connect(str(db))
    _seed_dar_table(conn, [
        {"id": "DAR-01", "analysis_type": "completeness",
         "source_tables": "ekpo", "superseded_by": ""},
    ])
    conn.close()

    monkeypatch.setattr(ca, "DB_PATH", db)
    assert ca.has_prior_dars_for_scope(["ekpo"]) is True


def test_helper_ignores_superseded_rows(tmp_path, monkeypatch):
    """Superseded DARs don't count — helper only sees current rows."""
    db = tmp_path / "probe.duckdb"
    conn = duckdb.connect(str(db))
    _seed_dar_table(conn, [
        {"id": "DAR-01", "analysis_type": "completeness",
         "source_tables": "ekpo", "superseded_by": "DAR-99"},
    ])
    conn.close()

    monkeypatch.setattr(ca, "DB_PATH", db)
    assert ca.has_prior_dars_for_scope(["ekpo"]) is False


def test_helper_empty_scope_returns_false():
    assert ca.has_prior_dars_for_scope([]) is False


# =========================================================================
# per-analyzer: fresh-table path must pass strict=False
# =========================================================================


class _StopAfterCapture(Exception):
    """Raised from the fake assemble_context to short-circuit the flow
    after the strict argument is captured."""


def _make_fake_assemble_context(captured: dict):
    def fake(**kwargs):
        captured.update(kwargs)
        raise _StopAfterCapture()
    return fake


@pytest.fixture
def fresh_env(monkeypatch):
    """Set a fake API key so the analyzer doesn't short-circuit before
    the assemble_context call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-for-tests")


def _assert_fresh_table_path_uses_strict_false(analyzer_mod, invoke_run):
    captured: dict = {}
    import unittest.mock as mock
    with mock.patch.object(analyzer_mod, "has_prior_dars_for_scope",
                           return_value=False), \
         mock.patch.object(analyzer_mod, "assemble_context",
                           side_effect=_make_fake_assemble_context(captured)):
        try:
            invoke_run()
        except _StopAfterCapture:
            pass
    assert "strict" in captured, "assemble_context was never called"
    assert captured["strict"] is False, (
        f"expected strict=False on fresh-table path; got {captured['strict']!r}"
    )


def test_completeness_fresh_table_uses_strict_false(fresh_env):
    import run_completeness_analysis as mod
    _assert_fresh_table_path_uses_strict_false(
        mod, lambda: mod.run("fresh_table", None, False),
    )


def test_dimensions_fresh_table_uses_strict_false(fresh_env):
    import run_dimensions_analysis as mod
    _assert_fresh_table_path_uses_strict_false(
        mod, lambda: mod.run("fresh_table", None, False),
    )


def test_magnitude_fresh_table_uses_strict_false(fresh_env):
    import run_magnitude_analysis as mod
    _assert_fresh_table_path_uses_strict_false(
        mod, lambda: mod.run("fresh_table", None, False),
    )


def test_code_tables_fresh_table_uses_strict_false(fresh_env):
    import run_code_tables_analysis as mod
    _assert_fresh_table_path_uses_strict_false(
        mod, lambda: mod.run("fresh_table", None, None, False),
    )
