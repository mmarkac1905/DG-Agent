"""Direction F.1 — cardinality DARs in Create S2T context bundle.

Tests verify that:
  - `_load_create_s2t_cardinality` renders the cardinality block via
    `_render_join_cardinality_block` reuse, with the brief's header.
  - `_load_dynamic` includes the cardinality block for purpose values
    {'create_s2t', 'pre_s2t_reasoning'} and only those.
  - The integration is graceful when no cardinality DARs exist for the
    scope (no crash, empty/skipped block).

Tests run against a focused in-memory DuckDB fixture (not the live DB
seed) so they are self-contained and re-runnable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _context_assembler as ca  # noqa: E402


@pytest.fixture
def cardinality_fixture():
    """In-memory DuckDB with the schema slice F.1 exercises."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    for t in ("equi", "seri", "mseg", "objk", "mkpf"):
        conn.execute(f"CREATE TABLE raw_sap.{t} (col VARCHAR)")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, analysis_type VARCHAR,
            executed_at_utc TIMESTAMP, result_json VARCHAR,
            status VARCHAR, superseded_by VARCHAR,
            source_tables VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.business_term_analysis_results (
            id VARCHAR, business_term_id VARCHAR,
            analysis_type VARCHAR, source_tables VARCHAR,
            result_json VARCHAR, executed_at_utc TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.analysis_findings (
            id VARCHAR, finding_type VARCHAR,
            query_description VARCHAR, result_summary VARCHAR,
            tables_explored VARCHAR, business_term_id VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.term_analysis_results (
            id VARCHAR, business_term_id VARCHAR,
            analysis_type VARCHAR, source_tables VARCHAR,
            result_json VARCHAR, executed_at_utc TIMESTAMP
        )
    """)
    yield conn
    conn.close()


def _add_cardinality_dar(conn, dar_id: str, source_tables: str,
                         finding: dict, status: str = "success",
                         superseded_by: str = ""):
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results "
        "(id, analysis_type, executed_at_utc, result_json, status, "
        " superseded_by, source_tables) "
        "VALUES (?, 'join_cardinality', CURRENT_TIMESTAMP, ?, ?, ?, ?)",
        [dar_id, json.dumps(finding), status, superseded_by, source_tables],
    )


def _seed_bg027_cardinality(conn):
    """Insert a minimal but representative set of cardinality DARs:

      - DAR-PRK1   equi<->mseg  per_record_key bridge via seri (EQUNR/MBLNR)
      - DAR-CAT1   equi<->mseg  catastrophic_fanout direct via MATNR
      - DAR-PRK2   equi<->objk  per_record_key direct via EQUNR
      - DAR-NS1    equi<->mseg  no_signal bridge via lqua (sparse data)
    """
    _add_cardinality_dar(conn, "DAR-PRK1", "equi,mseg", {
        "t1": "equi", "t2": "mseg", "kind": "bridge", "bridge_via": "seri",
        "key_columns_t1": ["EQUNR"], "key_columns_t2": ["MBLNR"],
        "bridge_keys_left": ["EQUNR"], "bridge_keys_right": ["MBLNR"],
        "fanout_class": "per_record_key",
        "avg_fanout": 1.0, "stddev_fanout": 0.0, "matched_keys_ratio": 1.0,
        "sample_size": 500, "sample_saturated": False,
        "matched_keys": 500, "max_fanout": 1, "source": ["shared_name"],
        "referential_integrity_pct": 100.0,
        "source_row_counts": {"equi": 45000, "mseg": 31965},
    })
    _add_cardinality_dar(conn, "DAR-CAT1", "equi,mseg", {
        "t1": "equi", "t2": "mseg", "kind": "direct", "bridge_via": None,
        "key_columns_t1": ["MATNR"], "key_columns_t2": ["MATNR"],
        "fanout_class": "catastrophic_fanout",
        "avg_fanout": 4500.0, "stddev_fanout": 6488.22, "matched_keys_ratio": 1.0,
        "sample_size": 10, "sample_saturated": True,
        "matched_keys": 10, "max_fanout": 22537, "source": ["shared_name"],
        "referential_integrity_pct": 100.0,
        "source_row_counts": {"equi": 45000, "mseg": 31965},
    })
    _add_cardinality_dar(conn, "DAR-PRK2", "equi,objk", {
        "t1": "equi", "t2": "objk", "kind": "direct", "bridge_via": None,
        "key_columns_t1": ["EQUNR"], "key_columns_t2": ["EQUNR"],
        "fanout_class": "per_record_key",
        "avg_fanout": 1.0, "stddev_fanout": 0.0, "matched_keys_ratio": 1.0,
        "sample_size": 500, "sample_saturated": False,
        "matched_keys": 500, "max_fanout": 1, "source": ["shared_name"],
        "referential_integrity_pct": 100.0,
        "source_row_counts": {"equi": 45000, "objk": 45000},
    })
    _add_cardinality_dar(conn, "DAR-NS1", "equi,mseg", {
        "t1": "equi", "t2": "mseg", "kind": "bridge", "bridge_via": "lqua",
        "key_columns_t1": ["MATNR"], "key_columns_t2": ["MATNR"],
        "bridge_keys_left": ["MATNR"], "bridge_keys_right": ["MATNR"],
        "fanout_class": "no_signal",
        "avg_fanout": 0.0, "stddev_fanout": 0.0, "matched_keys_ratio": 0.0,
        "sample_size": 500, "sample_saturated": False,
        "matched_keys": 0, "max_fanout": 0, "source": ["shared_name"],
        "referential_integrity_pct": 100.0,
        "source_row_counts": {"equi": 45000, "mseg": 31965},
    })


# ─── Tests ─────────────────────────────────────────────────────────────

def test_load_create_s2t_cardinality_renders_block(cardinality_fixture):
    """F.1.1 — sub-loader produces the brief's header + body content."""
    conn = cardinality_fixture
    _seed_bg027_cardinality(conn)
    block = ca._load_create_s2t_cardinality(
        ["equi", "mseg", "objk", "seri"], conn,
    )
    assert block, "expected non-empty cardinality block"
    assert block.startswith("## join_cardinality (scope-filtered, "
                            "evidence-prioritized)"), (
        f"expected brief's header prefix; got: {block[:120]!r}"
    )
    # Per-pair classifications appear in the body.
    assert "per_record_key" in block
    assert "catastrophic_fanout" in block
    assert "DAR-PRK1" in block
    assert "DAR-CAT1" in block
    # The brief's prioritization: per_record_key listed before
    # catastrophic_fanout for the same pair.
    prk1 = block.find("DAR-PRK1")
    cat1 = block.find("DAR-CAT1")
    assert 0 < prk1 < cat1, (
        f"per_record_key (DAR-PRK1) should render before "
        f"catastrophic_fanout (DAR-CAT1); got prk1={prk1} cat1={cat1}"
    )


def test_load_dynamic_includes_cardinality_for_create_s2t(cardinality_fixture):
    """F.1.2 — dynamic loader prepends the cardinality block when
    purpose='create_s2t'.
    """
    conn = cardinality_fixture
    _seed_bg027_cardinality(conn)
    content, toks, details = ca._load_dynamic(
        conn, scope=["equi", "mseg", "objk", "seri"], term_id="BG027",
        budget=10000, purpose="create_s2t",
    )
    assert "join_cardinality (scope-filtered" in content, (
        f"expected cardinality header in dynamic content; got first 200 chars: "
        f"{content[:200]!r}"
    )
    assert details.get("join_cardinality_rendered") is True
    # The block must render BEFORE the generic DAR dump.
    card_idx = content.find("join_cardinality (scope-filtered")
    generic_idx = content.find("## domain_analysis_results")
    if generic_idx >= 0:
        assert card_idx < generic_idx, (
            "cardinality block must render before generic DAR dump"
        )


def test_load_dynamic_includes_cardinality_for_pre_s2t_reasoning(
    cardinality_fixture,
):
    """F.1.2 — pre_s2t_reasoning is the second purpose that gets the block."""
    conn = cardinality_fixture
    _seed_bg027_cardinality(conn)
    content, _, details = ca._load_dynamic(
        conn, scope=["equi", "mseg", "objk", "seri"], term_id="BG027",
        budget=10000, purpose="pre_s2t_reasoning",
    )
    assert "join_cardinality (scope-filtered" in content
    assert details.get("join_cardinality_rendered") is True


def test_load_dynamic_excludes_cardinality_for_other_purposes(
    cardinality_fixture,
):
    """F.1.2 — other purposes (e.g., eda_classification) get the legacy
    generic DAR dump only; cardinality block does NOT render.
    """
    conn = cardinality_fixture
    _seed_bg027_cardinality(conn)
    for purpose in ("eda_classification", "eda_sql_generation",
                    "storytelling", "chat_followup"):
        content, _, details = ca._load_dynamic(
            conn, scope=["equi", "mseg", "objk", "seri"], term_id="BG027",
            budget=10000, purpose=purpose,
        )
        assert "join_cardinality (scope-filtered" not in content, (
            f"purpose={purpose!r} should NOT include cardinality block"
        )
        assert details.get("join_cardinality_rendered") is False


def test_load_dynamic_no_purpose_excludes_cardinality(cardinality_fixture):
    """Defensive: when purpose is None (legacy callers), the cardinality
    block is not added — preserves backward compatibility.
    """
    conn = cardinality_fixture
    _seed_bg027_cardinality(conn)
    content, _, details = ca._load_dynamic(
        conn, scope=["equi", "mseg"], term_id=None, budget=10000,
        purpose=None,
    )
    assert "join_cardinality (scope-filtered" not in content
    assert details.get("join_cardinality_rendered") is False


def test_no_cardinality_dars_renders_empty(cardinality_fixture):
    """F.1.3 — when no join_cardinality DARs exist for the scope, the
    sub-loader returns empty; _load_dynamic skips silently.
    """
    conn = cardinality_fixture  # no DARs seeded
    block = ca._load_create_s2t_cardinality(["equi", "mseg"], conn)
    assert block == "", f"expected empty string; got {block!r}"

    content, _, details = ca._load_dynamic(
        conn, scope=["equi", "mseg"], term_id="BG027", budget=10000,
        purpose="create_s2t",
    )
    assert "join_cardinality (scope-filtered" not in content
    assert details.get("join_cardinality_rendered") is False


def test_load_create_s2t_cardinality_empty_scope_returns_empty(
    cardinality_fixture,
):
    """Defensive: empty scope_tables -> empty block."""
    conn = cardinality_fixture
    _seed_bg027_cardinality(conn)
    assert ca._load_create_s2t_cardinality([], conn) == ""


def test_load_create_s2t_cardinality_filters_by_scope(cardinality_fixture):
    """F10-aware lookup — DARs whose tables don't overlap scope are
    excluded.
    """
    conn = cardinality_fixture
    _seed_bg027_cardinality(conn)
    # Scope to only equi+objk → equi/mseg DARs should be filtered out.
    block = ca._load_create_s2t_cardinality(["equi", "objk"], conn)
    assert "DAR-PRK2" in block, "equi/objk DAR should appear"
    assert "DAR-PRK1" not in block, "equi/mseg DAR should be filtered"
    assert "DAR-CAT1" not in block
