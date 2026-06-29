"""Direction F.4 — retry-on-F.3-rejection tests.

Verifies the bounded self-correcting loop in create_s2t_with_implementation:
  - On valid SQL first try, no retry fires; _f3_attempts=1.
  - On F.3 rejection then valid SQL on retry, _f3_attempts=2 and the
    second LLM call's user_prompt contains the rejection hint.
  - On F.3 rejection both attempts, return {"error": ...,
    "_f3_attempts": 2} citing the catastrophic-join-rejected-after-retry
    error.

LLM is mocked at the `_post_claude` boundary so tests run without
network. Cardinality DAR fixture and the F.3 validator are exercised
through the live module path for fidelity.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "app"))

import claude_api as ca  # noqa: E402
import _s2t_cardinality_validator as v  # noqa: E402


# ─── Fixture: minimal in-memory DB the validator can read DARs from ───

@pytest.fixture
def mocked_db(tmp_path, monkeypatch):
    """Builds an isolated DB at tmp_path/cpe_analytics.duckdb with the
    minimum schema F.3 / F.4 need:
      - main_seeds.domain_analysis_results (cardinality DARs)
      - main_seeds.business_glossary, main_seeds.s2t_mapping (term scope
        for _resolve_term_scope)
    Then monkeypatches Path resolution inside claude_api so its
    _val_conn opens this DB instead of the project DB.
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

    # Seed cardinality DARs:
    # equi-mseg via MATNR is catastrophic (DAR-CAT)
    # equi-objk via EQUNR is per_record_key (DAR-PRK)
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results VALUES "
        "(?, 'join_cardinality', CURRENT_TIMESTAMP, ?, 'success', '', ?)",
        ["DAR-CAT", json.dumps({
            "t1": "equi", "t2": "mseg", "kind": "direct",
            "key_columns_t1": ["MATNR"], "key_columns_t2": ["MATNR"],
            "fanout_class": "catastrophic_fanout",
            "avg_fanout": 4500.0, "stddev_fanout": 6488.22,
            "matched_keys_ratio": 1.0,
        }), "equi,mseg"],
    )
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results VALUES "
        "(?, 'join_cardinality', CURRENT_TIMESTAMP, ?, 'success', '', ?)",
        ["DAR-PRK", json.dumps({
            "t1": "equi", "t2": "objk", "kind": "direct",
            "key_columns_t1": ["EQUNR"], "key_columns_t2": ["EQUNR"],
            "fanout_class": "per_record_key",
            "avg_fanout": 1.0, "stddev_fanout": 0.0,
            "matched_keys_ratio": 1.0,
        }), "equi,objk"],
    )

    conn.execute(
        "INSERT INTO main_seeds.business_glossary VALUES "
        "('BG-TEST', 'scope_confirmed', '{}')"
    )
    for t in ("equi", "objk", "mseg"):
        conn.execute(
            "INSERT INTO main_seeds.s2t_mapping VALUES ('BG-TEST', ?)", [t],
        )
    conn.close()

    # Patch claude_api so its _val_conn / scope-resolution paths point
    # at this DB. claude_api.create_s2t_with_implementation builds the
    # _val_conn path as `Path(__file__).parent.parent / "cpe_analytics.duckdb"`.
    # The simplest hook is to monkeypatch the Path call site by patching
    # `Path` inside claude_api. We instead just patch _resolve_term_scope
    # and validate_s2t_sql + lazy trigger to use our DB.

    # Stub _resolve_term_scope to return our scope without touching the
    # production conn.
    monkeypatch.setattr(
        ca, "_resolve_term_scope", lambda term_id: ["equi", "objk", "mseg"],
    )

    # Patch validate_s2t_sql call site inside claude_api to use our DB.
    real_validate = v.validate_s2t_sql

    def _validate_via_test_db(sql, scope_tables, conn=None):
        c = duckdb.connect(str(db_path), read_only=True)
        try:
            return real_validate(sql, scope_tables, c)
        finally:
            c.close()

    # We want claude_api's import-and-call to land here. Patch the
    # module-level reference. The from-import happens inside the function;
    # so we patch the source module instead.
    monkeypatch.setattr(v, "validate_s2t_sql", _validate_via_test_db)

    # Disable lazy analysis (tests never want to fire run_join_cardinality).
    monkeypatch.setattr(v, "trigger_lazy_analysis",
                        lambda t1, t2, conn: False)

    # Stub assemble_context to return a tiny synthetic bundle so we don't
    # need the full live context.
    class _FakeBundle:
        formatted_prompt = "## Test bundle\n(empty)"
        token_count = 100
        debug = {"fingerprint": "deadbeef"}
        scope_resolution = {"strategy_used": "test",
                            "resolved_tables": ["equi", "objk", "mseg"]}

    monkeypatch.setattr(
        sys.modules["_context_assembler"]
        if "_context_assembler" in sys.modules else None,
        "assemble_context", lambda **kw: _FakeBundle(),
    ) if "_context_assembler" in sys.modules else None
    # Cleaner: import + patch
    import _context_assembler
    monkeypatch.setattr(_context_assembler, "assemble_context",
                        lambda **kw: _FakeBundle())

    # Stub BAR resolver to no-op.
    import _bar_consumer
    monkeypatch.setattr(_bar_consumer, "resolve_promoted_bar",
                        lambda *a, **kw: None)

    # Skip the citation audit (it runs greps that don't matter here).
    monkeypatch.setattr(ca, "_audit_s2t_citations",
                        lambda result, bundle_text="": result)

    # Set API_KEY so the function doesn't bail at the env check.
    monkeypatch.setattr(ca, "API_KEY", "test-key")

    yield db_path


# ─── Helpers ─────────────────────────────────────────────────────────

_BAD_SQL = """
WITH x AS (
  SELECT eq.EQUNR, m.BWART
  FROM {{ ref('stg_sap__equi') }} eq
  INNER JOIN {{ ref('stg_sap__mseg') }} m ON eq.MATNR = m.MATNR
)
SELECT COUNT(*) FROM x
"""

_GOOD_SQL = """
SELECT * FROM {{ ref('stg_sap__equi') }} eq
INNER JOIN {{ ref('stg_sap__objk') }} obj ON eq.EQUNR = obj.EQUNR
"""


def _make_llm_response(sql: str) -> dict:
    """Build a minimal valid create_s2t result dict."""
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
    }


def _kwargs():
    return dict(
        term_name="test_term",
        term_definition="test definition",
        term_unit="count",
        term_grain="per_thing",
        term_id="BG-TEST",
    )


# ─── Tests ────────────────────────────────────────────────────────────

def test_no_retry_on_first_attempt_pass(mocked_db):
    """Happy path: LLM returns valid SQL on attempt 1 → no retry, return
    result with _f3_attempts=1, and only one LLM call was made."""
    calls: list[dict] = []

    def fake_post(system_prompt, user_prompt, max_tokens=None):
        calls.append({"user_prompt": user_prompt})
        return _make_llm_response(_GOOD_SQL)

    with patch.object(ca, "_post_claude", side_effect=fake_post):
        result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" not in result
    assert result.get("_f3_validation_passed") is True
    assert result.get("_f3_attempts") == 1
    assert len(calls) == 1, f"expected 1 LLM call, got {len(calls)}"
    assert "Previous attempt rejected by F.3 validator" not in calls[0]["user_prompt"]


def test_retries_on_f3_rejection_then_passes(mocked_db):
    """Bad SQL on attempt 1, good SQL on attempt 2.
    Expect _f3_attempts=2, _f3_validation_passed=True, second prompt
    contains the rejection hint with DAR citation.
    """
    sql_sequence = [_BAD_SQL, _GOOD_SQL]
    calls: list[dict] = []

    def fake_post(system_prompt, user_prompt, max_tokens=None):
        calls.append({"user_prompt": user_prompt})
        sql = sql_sequence[len(calls) - 1]
        return _make_llm_response(sql)

    with patch.object(ca, "_post_claude", side_effect=fake_post):
        result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" not in result, f"unexpected error: {result.get('error')}"
    assert result.get("_f3_validation_passed") is True
    assert result.get("_f3_attempts") == 2
    assert len(calls) == 2

    # Second prompt must include the rejection hint.
    second_prompt = calls[1]["user_prompt"]
    assert "Previous attempt rejected by F.3 validator" in second_prompt
    assert "catastrophic_fanout" in second_prompt
    assert "DAR-CAT" in second_prompt  # the catastrophic DAR cited
    assert "Regenerate the SQL with corrected join keys" in second_prompt


def test_returns_error_after_max_retries(mocked_db):
    """Both attempts return catastrophic SQL → final result has
    'error' with 'catastrophic_join_rejected_after_retry' and
    _f3_attempts=2.
    """
    calls: list[dict] = []

    def fake_post(system_prompt, user_prompt, max_tokens=None):
        calls.append({"user_prompt": user_prompt})
        return _make_llm_response(_BAD_SQL)

    with patch.object(ca, "_post_claude", side_effect=fake_post):
        result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" in result
    assert "catastrophic_join_rejected_after_retry" in result["error"]
    assert result.get("_f3_attempts") == 2
    assert len(calls) == 2
    # Validation detail attached.
    assert "_f3_validation" in result
    assert result["_f3_validation"].get("status") == "rejected_catastrophic_join"


def test_returns_error_when_llm_returns_error(mocked_db):
    """If the LLM call itself errors (not F.3), no retry fires —
    surface the LLM error with attempt count for audit."""
    def fake_post(system_prompt, user_prompt, max_tokens=None):
        return {"error": "anthropic API timeout"}

    with patch.object(ca, "_post_claude", side_effect=fake_post):
        result = ca.create_s2t_with_implementation(**_kwargs())

    assert "error" in result
    assert "anthropic API timeout" in result["error"]
    assert result.get("_f3_attempts") == 1
