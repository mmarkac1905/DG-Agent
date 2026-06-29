"""C6 Finding D — BAR-status dispatcher branch tests.

Verifies the new dispatcher logic in create_s2t_with_implementation:
  - When a promoted BAR exists, BAR-consumer path runs (existing
    behavior preserved).
  - When the latest finished BAR has status='needs_data_extension'
    or 'hard_stop', the dispatcher returns a structured refusal
    with _refusal_kind='bar_needs_data_extension'.
  - When the latest BAR has a non-refusing status (converged_soft,
    failed-but-soft), the dispatcher falls through to the generator
    path (existing behavior preserved).
  - When no BAR exists, the dispatcher falls through to the
    generator path (existing behavior preserved).
  - bridge_violations from the latest iteration_trace are surfaced
    in the refusal payload.

Mocks resolve_promoted_bar / resolve_latest_bar at the
_bar_consumer module boundary; LLM call is mocked so no live call
fires (the test never actually runs the generator path).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "app"))

import claude_api as ca  # noqa: E402
import _bar_consumer as bc  # noqa: E402


@pytest.fixture
def mocked_env(tmp_path, monkeypatch):
    """Reuse the bridge-coverage fixture's scaffolding (scope, bundle,
    audit, API key)."""
    db_path = tmp_path / "cpe_analytics.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.business_glossary (id VARCHAR, status VARCHAR,
        scope_derivation_history_json VARCHAR)""")
    conn.execute("""
        CREATE TABLE main_seeds.s2t_mapping (business_term_id VARCHAR,
        source_table VARCHAR)""")
    conn.execute(
        "INSERT INTO main_seeds.business_glossary VALUES "
        "('BG-TEST', 'scope_confirmed', '{}')"
    )
    for t in ("equi", "objk"):
        conn.execute(
            "INSERT INTO main_seeds.s2t_mapping VALUES ('BG-TEST', ?)", [t],
        )
    conn.close()

    monkeypatch.setattr(
        ca, "_resolve_term_scope", lambda term_id: ["equi", "objk"],
    )

    class _FakeBundle:
        formatted_prompt = "## Test bundle"
        token_count = 100
        debug = {"fingerprint": "deadbeef"}
        scope_resolution = {"strategy_used": "test",
                            "resolved_tables": ["equi", "objk"]}

    import _context_assembler
    monkeypatch.setattr(_context_assembler, "assemble_context",
                        lambda **kw: _FakeBundle())

    monkeypatch.setattr(ca, "_audit_s2t_citations",
                        lambda result, bundle_text="": result)
    monkeypatch.setattr(ca, "API_KEY", "test-key")

    yield


def _kwargs():
    return dict(
        term_name="test_term",
        term_definition="test definition",
        term_unit="count",
        term_grain="per_thing",
        term_id="BG-TEST",
    )


# ─── Tests ────────────────────────────────────────────────────────────

def test_dispatcher_uses_promoted_when_present(mocked_env):
    """Existing behavior preserved: when resolve_promoted_bar returns a
    non-None PromotedBarInput, the BAR-consumer path runs and the
    needs_data_extension branch is skipped."""
    fake_promoted = bc.PromotedBarInput(
        bar_id="BAR-PROMOTED",
        term_id="BG-TEST",
        final_query_sql="SELECT 1",
        dbt_semantic_model_consumed=["stg_sap__equi"],
        term_conditions_covered=["test"],
        final_metric_interpretation="test interp",
    )
    consumer_mock = MagicMock(return_value={"source": "promoted_bar",
                                            "bar_id": "BAR-PROMOTED",
                                            "dbt_models": []})
    latest_mock = MagicMock(return_value={"id": "SHOULD-NOT-BE-USED",
                                          "status": "needs_data_extension"})
    with patch.object(bc, "resolve_promoted_bar", return_value=fake_promoted):
        with patch.object(bc, "resolve_latest_bar", latest_mock):
            with patch.object(ca, "create_s2t_from_promoted_bar",
                              consumer_mock):
                result = ca.create_s2t_with_implementation(**_kwargs())

    assert result["source"] == "promoted_bar"
    assert result["bar_id"] == "BAR-PROMOTED"
    # The needs_data_extension branch is gated on `promoted is None`;
    # latest_bar is never queried when promoted path runs.
    assert latest_mock.call_count == 0


def test_dispatcher_refuses_on_needs_data_extension(mocked_env):
    """Latest BAR has status='needs_data_extension' → dispatcher returns
    structured refusal without calling the LLM."""
    latest_bar = {
        "id": "BAR-00010",
        "business_term_id": "BG-TEST",
        "status": "needs_data_extension",
        "convergence_reason": "hard_stop_bridge_unreachable",
        "sourcing_recommendations": {"summary": {"total_recommendations": 5}},
        "iteration_trace": [],
        "bridge_coverage_consulted_raw": None,
        "finished_at_utc": "2026-04-28 11:00:00",
    }
    llm_mock = MagicMock()
    with patch.object(bc, "resolve_promoted_bar", return_value=None):
        with patch.object(bc, "resolve_latest_bar", return_value=latest_bar):
            with patch.object(ca, "_post_claude", llm_mock):
                result = ca.create_s2t_with_implementation(**_kwargs())

    assert result["error"] == "stage_e_refused_bar_needs_data_extension"
    assert result["_refusal_kind"] == "bar_needs_data_extension"
    assert result["_bar_id"] == "BAR-00010"
    assert result["_bar_status"] == "needs_data_extension"
    assert result["_bar_convergence_reason"] == "hard_stop_bridge_unreachable"
    # LLM must not have been invoked.
    assert llm_mock.call_count == 0


def test_dispatcher_refuses_on_hard_stop(mocked_env):
    """Latest BAR has status='hard_stop' → also triggers refusal."""
    latest_bar = {
        "id": "BAR-00006",
        "business_term_id": "BG-TEST",
        "status": "hard_stop",
        "convergence_reason": "hard_stop_bridge_unreachable",
        "sourcing_recommendations": None,
        "iteration_trace": [],
        "bridge_coverage_consulted_raw": None,
        "finished_at_utc": "2026-04-25 11:00:00",
    }
    llm_mock = MagicMock()
    with patch.object(bc, "resolve_promoted_bar", return_value=None):
        with patch.object(bc, "resolve_latest_bar", return_value=latest_bar):
            with patch.object(ca, "_post_claude", llm_mock):
                result = ca.create_s2t_with_implementation(**_kwargs())

    assert result["error"] == "stage_e_refused_bar_hard_stop"
    assert result["_refusal_kind"] == "bar_needs_data_extension"
    assert result["_bar_status"] == "hard_stop"
    assert llm_mock.call_count == 0


def test_dispatcher_falls_through_when_latest_is_converged_soft(mocked_env):
    """Latest BAR has status='converged' (success) — generator path
    runs normally. We mock the LLM to return a clean response and
    assert the dispatcher invoked it (i.e., did NOT refuse)."""
    latest_bar = {
        "id": "BAR-00009",
        "business_term_id": "BG-TEST",
        "status": "converged",
        "convergence_reason": "converged_soft",
        "sourcing_recommendations": None,
        "iteration_trace": [],
        "bridge_coverage_consulted_raw": None,
        "finished_at_utc": "2026-04-26 11:00:00",
    }
    llm_mock = MagicMock(return_value={"error": "stub-LLM-skip"})
    with patch.object(bc, "resolve_promoted_bar", return_value=None):
        with patch.object(bc, "resolve_latest_bar", return_value=latest_bar):
            with patch.object(ca, "_post_claude", llm_mock):
                result = ca.create_s2t_with_implementation(**_kwargs())

    # Dispatcher fell through to generator; LLM was invoked.
    assert llm_mock.call_count >= 1
    # No refusal.
    assert result.get("_refusal_kind") != "bar_needs_data_extension"


def test_dispatcher_falls_through_when_no_bar(mocked_env):
    """No BAR exists for the term → generator path runs."""
    llm_mock = MagicMock(return_value={"error": "stub-LLM-skip"})
    with patch.object(bc, "resolve_promoted_bar", return_value=None):
        with patch.object(bc, "resolve_latest_bar", return_value=None):
            with patch.object(ca, "_post_claude", llm_mock):
                result = ca.create_s2t_with_implementation(**_kwargs())

    assert llm_mock.call_count >= 1
    assert result.get("_refusal_kind") != "bar_needs_data_extension"


def test_refusal_includes_bridge_violations_from_iteration_trace(mocked_env):
    """When the latest BAR's last iteration has gates_result.bridge_violations,
    the refusal payload surfaces them so the UI can render context."""
    bridge_violations = [
        "[DAR-00527] mseg.BWART='201' unreachable through "
        "mseg->seri bridge (reachable: ['101'])"
    ]
    latest_bar = {
        "id": "BAR-00010",
        "business_term_id": "BG-TEST",
        "status": "needs_data_extension",
        "convergence_reason": "hard_stop_bridge_unreachable",
        "sourcing_recommendations": None,
        "iteration_trace": [
            {"iteration": 1,
             "gates_result": {"bridge_violations": bridge_violations}}
        ],
        "bridge_coverage_consulted_raw": None,
        "finished_at_utc": "2026-04-28 11:00:00",
    }
    with patch.object(bc, "resolve_promoted_bar", return_value=None):
        with patch.object(bc, "resolve_latest_bar", return_value=latest_bar):
            with patch.object(ca, "_post_claude", MagicMock()):
                result = ca.create_s2t_with_implementation(**_kwargs())

    assert result["_refusal_kind"] == "bar_needs_data_extension"
    assert result["_bridge_violations"] == bridge_violations
