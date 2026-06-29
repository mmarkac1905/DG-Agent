"""C6 — bridge_coverage_consulted attestation audit tests.

Verifies the post-LLM-emit audit hook in
`create_s2t_with_implementation` that calls
`_check_bridge_coverage_attestation` and surfaces failures via the
existing `_citation_audit_issues` + `llm_self_attestation_mismatch`
channels.

The audit is a soft signal: failures don't refuse the dispatch (the
data-side `bridge_coverage_gate` is the refuse channel). Tests
verify the channels populate correctly.
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
import _bridge_coverage_gate as bcg  # noqa: E402
import _s2t_cardinality_validator as v  # noqa: E402


_GOOD_SQL = """
SELECT * FROM {{ ref('stg_sap__equi') }} eq
INNER JOIN {{ ref('stg_sap__objk') }} obj ON eq.EQUNR = obj.EQUNR
"""


def _make_llm_response(consulted=None) -> dict:
    return {
        "s2t_mapping": [],
        "transformation_plain": "test",
        "transformation_sql": _GOOD_SQL,
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
        "bridge_coverage_consulted": consulted if consulted is not None else [],
    }


@pytest.fixture
def mocked_env(tmp_path, monkeypatch):
    """Same scaffolding as the gate tests; bridge_coverage_gate +
    _check_bridge_coverage_attestation are NOT pre-mocked here so each
    test can drive them individually."""
    db_path = tmp_path / "cpe_analytics.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    for t in ("equi", "objk"):
        conn.execute(f"CREATE TABLE raw_sap.{t} (col VARCHAR)")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, analysis_type VARCHAR, executed_at_utc TIMESTAMP,
            result_json VARCHAR, status VARCHAR, superseded_by VARCHAR,
            source_tables VARCHAR
        )
    """)
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
        formatted_prompt = "## Test bundle"
        token_count = 100
        debug = {"fingerprint": "deadbeef"}
        scope_resolution = {"strategy_used": "test",
                            "resolved_tables": ["equi", "objk"]}

    import _context_assembler
    monkeypatch.setattr(_context_assembler, "assemble_context",
                        lambda **kw: _FakeBundle())

    import _bar_consumer
    monkeypatch.setattr(_bar_consumer, "resolve_promoted_bar",
                        lambda *a, **kw: None)
    monkeypatch.setattr(_bar_consumer, "resolve_latest_bar",
                        lambda *a, **kw: None)

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

def test_bridge_coverage_consulted_passes_when_dars_cited(mocked_env):
    """Audit returns ok=True when LLM cited at least one DAR (even if
    the DAR list contains entries). Mock the audit helper to return
    (True, None) and verify no audit issues are appended."""
    audit_mock = MagicMock(return_value=(True, None))
    gate_mock = MagicMock(return_value=(True, [], "pass"))
    with patch.object(bcg, "_check_bridge_coverage_attestation", audit_mock):
        with patch.object(bcg, "bridge_coverage_gate", gate_mock):
            with patch.object(ca, "_post_claude",
                              return_value=_make_llm_response(
                                  consulted=["DAR-00527"])):
                result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" not in result
    assert result.get("llm_self_attestation_mismatch") is not True
    assert audit_mock.call_count == 1


def test_bridge_coverage_consulted_audit_fails_when_empty_with_dars_in_scope(mocked_env):
    """Audit returns ok=False with error_msg → audit issue gets appended
    + llm_self_attestation_mismatch flips to True. Dispatch still
    completes (audit is a soft signal)."""
    err = ("bridge_coverage_consulted is empty but 3 "
           "bridge_coverage_by_filter DAR(s) are in scope")
    audit_mock = MagicMock(return_value=(False, err))
    gate_mock = MagicMock(return_value=(True, [], "pass"))
    with patch.object(bcg, "_check_bridge_coverage_attestation", audit_mock):
        with patch.object(bcg, "bridge_coverage_gate", gate_mock):
            with patch.object(ca, "_post_claude",
                              return_value=_make_llm_response(consulted=[])):
                result = ca.create_s2t_with_implementation(**_kwargs())

    # Soft signal: dispatch completes (no error key from refusal).
    assert "error" not in result
    # Audit issue propagated.
    issues = result.get("_citation_audit_issues") or []
    assert err in issues
    assert result.get("llm_self_attestation_mismatch") is True


def test_bridge_coverage_consulted_audit_passes_when_empty_with_no_dars(mocked_env):
    """When no DARs exist in scope, an empty bridge_coverage_consulted
    is fine (audit returns ok=True, error_msg=None). No audit issues
    added; mismatch flag not set."""
    audit_mock = MagicMock(return_value=(True, None))
    gate_mock = MagicMock(return_value=(True, [], "skipped_no_dars"))
    with patch.object(bcg, "_check_bridge_coverage_attestation", audit_mock):
        with patch.object(bcg, "bridge_coverage_gate", gate_mock):
            with patch.object(ca, "_post_claude",
                              return_value=_make_llm_response(consulted=[])):
                result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" not in result
    issues = result.get("_citation_audit_issues") or []
    # No audit issue appended (the helper returned ok=True).
    bridge_issues = [i for i in issues if "bridge_coverage_consulted" in i]
    assert bridge_issues == []
    assert result.get("llm_self_attestation_mismatch") is not True


def test_attestation_appended_to_existing_citation_audit_channel(mocked_env):
    """If `_audit_s2t_citations` had already populated
    `_citation_audit_issues`, the bridge-coverage audit appends to the
    existing list (doesn't overwrite)."""
    err = "bridge_coverage_consulted is empty"

    def _augmented_audit(result, bundle_text=""):
        existing = ["pre-existing-citation-issue"]
        result["_citation_audit_issues"] = existing
        return result

    audit_mock = MagicMock(return_value=(False, err))
    gate_mock = MagicMock(return_value=(True, [], "pass"))
    with patch.object(ca, "_audit_s2t_citations", _augmented_audit):
        with patch.object(bcg, "_check_bridge_coverage_attestation", audit_mock):
            with patch.object(bcg, "bridge_coverage_gate", gate_mock):
                with patch.object(ca, "_post_claude",
                                  return_value=_make_llm_response(consulted=[])):
                    result = ca.create_s2t_with_implementation(**_kwargs())

    issues = result.get("_citation_audit_issues") or []
    # Both the pre-existing issue AND the new bridge issue are present.
    assert "pre-existing-citation-issue" in issues
    assert err in issues
    assert result.get("llm_self_attestation_mismatch") is True
