"""C6 — Stage E bridge_coverage gate invocation tests.

Verifies the gate-call shape inside create_s2t_with_implementation:
  - Gate fires post-LLM-emit, BEFORE F.3.
  - On (passed=False, violations=[...]) the dispatcher returns
    a structured refusal with _refusal_kind='bridge_coverage_violation'.
  - On (passed=True) flow continues to F.3.
  - When the gate raises, the dispatcher logs warning and skips
    enforcement (no crash, falls through to F.3).
  - Placement: when both bridge and cardinality violations exist,
    bridge fires first (F.3 not invoked).

Mocks `bridge_coverage_gate` directly to control the (passed,
violations, status) tuple per test. Reuses the F.4 retry fixture's
mocked_db scaffolding for scope/validator paths.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "app"))

import claude_api as ca  # noqa: E402
import _bridge_coverage_gate as bcg  # noqa: E402
import _s2t_cardinality_validator as v  # noqa: E402


_GOOD_SQL = """
SELECT * FROM {{ ref('stg_sap__equi') }} eq
INNER JOIN {{ ref('stg_sap__objk') }} obj ON eq.EQUNR = obj.EQUNR
"""


def _make_llm_response(sql: str = _GOOD_SQL) -> dict:
    return {
        "s2t_mapping": [],
        "transformation_plain": "test",
        "transformation_sql": sql,
        "implementation_plan": {},
        "dbt_models": [],
        "warnings": [],
        "confidence": "high",
        "domain_facts_consumed": False, "domain_facts_citations": [],
        "analysis_findings_consumed": False, "analysis_findings_citations": [],
        "dar_consumed": False, "dar_citations": [],
        "bar_consumed": False, "bar_citations": [],
        "semantic_model_consumed": [],
        "dbt_semantic_model_consumed": [],
        "join_cardinality_consulted": [],
        "bridge_coverage_consulted": [],
    }


@pytest.fixture
def mocked_env(tmp_path, monkeypatch):
    """Mocks the surrounding env so create_s2t_with_implementation can
    run without LLM/DB. The bridge_coverage_gate is left UNMOCKED here;
    individual tests patch it via patch.object(bcg, ...).
    """
    db_path = tmp_path / "cpe_analytics.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    for t in ("equi", "objk", "seri", "mseg", "mkpf"):
        conn.execute(f"CREATE TABLE raw_sap.{t} (col VARCHAR)")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, analysis_type VARCHAR, executed_at_utc TIMESTAMP,
            result_json VARCHAR, status VARCHAR, superseded_by VARCHAR,
            source_tables VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.business_glossary (
            id VARCHAR, status VARCHAR, scope_derivation_history_json VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.s2t_mapping (
            business_term_id VARCHAR, source_table VARCHAR
        )
    """)
    conn.execute(
        "INSERT INTO main_seeds.business_glossary VALUES "
        "('BG-TEST', 'scope_confirmed', '{}')"
    )
    for t in ("equi", "objk", "mseg"):
        conn.execute(
            "INSERT INTO main_seeds.s2t_mapping VALUES ('BG-TEST', ?)", [t],
        )
    conn.close()

    monkeypatch.setattr(
        ca, "_resolve_term_scope", lambda term_id: ["equi", "objk", "mseg"],
    )

    real_validate = v.validate_s2t_sql

    def _validate_via_test_db(sql, scope_tables, conn=None):
        c = duckdb.connect(str(db_path), read_only=True)
        try:
            return real_validate(sql, scope_tables, c)
        finally:
            c.close()

    monkeypatch.setattr(v, "validate_s2t_sql", _validate_via_test_db)
    monkeypatch.setattr(v, "trigger_lazy_analysis",
                        lambda t1, t2, conn: False)

    class _FakeBundle:
        formatted_prompt = "## Test bundle\n(empty)"
        token_count = 100
        debug = {"fingerprint": "deadbeef"}
        scope_resolution = {"strategy_used": "test",
                            "resolved_tables": ["equi", "objk", "mseg"]}

    import _context_assembler
    monkeypatch.setattr(_context_assembler, "assemble_context",
                        lambda **kw: _FakeBundle())

    import _bar_consumer
    monkeypatch.setattr(_bar_consumer, "resolve_promoted_bar",
                        lambda *a, **kw: None)
    # Step 4 dispatcher branch: stub the latest-BAR resolver so
    # tests that don't care about Finding D fall through cleanly.
    monkeypatch.setattr(_bar_consumer, "resolve_latest_bar",
                        lambda *a, **kw: None)

    monkeypatch.setattr(ca, "_audit_s2t_citations",
                        lambda result, bundle_text="": result)
    monkeypatch.setattr(ca, "API_KEY", "test-key")

    yield db_path


def _kwargs():
    return dict(
        term_name="test_term",
        term_definition="test definition",
        term_unit="count",
        term_grain="per_thing",
        term_id="BG-TEST",
    )


# ─── Tests ────────────────────────────────────────────────────────────

def test_gate_passes_when_no_violations(mocked_env):
    """Gate returns (True, [], 'pass') → flow continues to F.3."""
    gate_mock = MagicMock(return_value=(True, [], "pass"))
    with patch.object(bcg, "bridge_coverage_gate", gate_mock):
        with patch.object(ca, "_post_claude",
                          return_value=_make_llm_response()):
            result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" not in result, f"unexpected error: {result.get('error')}"
    assert result.get("_bridge_coverage_gate_status") == "pass"
    # F.3 ran (we made it past the bridge gate).
    assert result.get("_f3_validation_passed") is True
    assert gate_mock.call_count == 1


def test_gate_refuses_when_filter_unreachable(mocked_env):
    """Gate returns (False, [violations], 'fail') → dispatcher returns
    structured refusal with _refusal_kind='bridge_coverage_violation'."""
    violations = [
        "[DAR-00527] mseg.BWART='201' unreachable through "
        "mseg->seri bridge (reachable: ['101'])"
    ]
    gate_mock = MagicMock(return_value=(False, violations, "fail"))
    f3_mock = MagicMock(
        return_value={"status": "passed",
                      "reason": "should_not_be_called"}
    )
    with patch.object(bcg, "bridge_coverage_gate", gate_mock):
        with patch.object(v, "validate_s2t_sql", f3_mock):
            with patch.object(ca, "_post_claude",
                              return_value=_make_llm_response()):
                result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" in result
    assert result["error"] == "stage_e_refused_bridge_coverage_violation"
    assert result["_refusal_kind"] == "bridge_coverage_violation"
    assert result["_bridge_violations"] == violations
    assert result["_bridge_gate_status"] == "fail"
    assert result["_attempted_sql"] == _GOOD_SQL
    # F.3 must NOT have been called — bridge gate fires first.
    assert f3_mock.call_count == 0


def test_gate_skipped_when_no_dars(mocked_env):
    """No bridge_coverage_by_filter DARs in scope → gate returns
    (True, [], 'skipped_no_dars') → flow continues."""
    gate_mock = MagicMock(return_value=(True, [], "skipped_no_dars"))
    with patch.object(bcg, "bridge_coverage_gate", gate_mock):
        with patch.object(ca, "_post_claude",
                          return_value=_make_llm_response()):
            result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" not in result
    assert result.get("_bridge_coverage_gate_status") == "skipped_no_dars"


def test_gate_failure_does_not_crash_dispatcher(mocked_env):
    """If bridge_coverage_gate raises, the dispatcher logs warning and
    falls through to F.3 — never crashes."""
    gate_mock = MagicMock(side_effect=RuntimeError("simulated gate crash"))
    with patch.object(bcg, "bridge_coverage_gate", gate_mock):
        with patch.object(ca, "_post_claude",
                          return_value=_make_llm_response()):
            result = ca.create_s2t_with_implementation(**_kwargs())

    # Dispatcher survived.
    assert "error" not in result, f"unexpected error: {result.get('error')}"
    # Status reflects the skip-on-crash behavior.
    assert result.get("_bridge_coverage_gate_status") == "skipped_internal_error"
    # F.3 still ran.
    assert result.get("_f3_validation_passed") is True


def test_gate_placement_before_f3(mocked_env):
    """When the bridge gate refuses, F.3 must NOT execute regardless of
    whether the SQL would also have triggered F.3 (cardinality)."""
    gate_mock = MagicMock(return_value=(False, ["BWART='201' unreachable"], "fail"))
    f3_mock = MagicMock(
        return_value={"status": "rejected_catastrophic_join",
                      "hint": "would-have-fired"}
    )
    with patch.object(bcg, "bridge_coverage_gate", gate_mock):
        with patch.object(v, "validate_s2t_sql", f3_mock):
            with patch.object(ca, "_post_claude",
                              return_value=_make_llm_response()):
                result = ca.create_s2t_with_implementation(**_kwargs())

    # Bridge refusal wins; F.3 never fires.
    assert result["_refusal_kind"] == "bridge_coverage_violation"
    assert f3_mock.call_count == 0, (
        "F.3 must not run when the bridge gate has already refused"
    )
