"""Gate A unit tests for scripts/_context_assembler.py.

Covers: budget math (both worked examples from design §3d exactly,
plus all 5 purposes conservation); scope resolution S1-S3; fingerprint
stability; strict mode raises on empty HEAVY; non-HEAVY empty does
not raise.

All tests use the stub tokenizer (default). No LLM calls. No Anthropic
API dependency.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import _context_assembler as ca  # noqa: E402
from _context_assembler import (  # noqa: E402
    ContextDegradedError, ContextScopeError, LAYERS, PURPOSE_WEIGHTS,
    WEIGHT_UNITS, _layer_is_empty, assemble_context, compute_fingerprint,
    compute_layer_budgets, resolve_scope,
)


# =========================================================================
# §3d budget math — worked examples must match exactly
# =========================================================================

def test_budget_create_s2t_50k_matches_design_exactly():
    """Design §3d worked example 1."""
    b = compute_layer_budgets("create_s2t", 50_000)
    assert b["ontology"] == 12000
    assert b["static"] == 7500
    assert b["dynamic"] == 7500
    assert b["examples"] == 7500
    assert b["business"] == 7500
    assert b["archived"] == 3000
    assert sum(b.values()) == 45000


def test_budget_eda_sql_generation_20k_matches_design_exactly():
    """Design §3d worked example 2."""
    b = compute_layer_budgets("eda_sql_generation", 20_000)
    assert b["static"] == 7200
    assert b["dynamic"] == 7200
    assert b["examples"] == 1800
    assert b["business"] == 1800
    assert b["ontology"] == 0
    assert b["archived"] == 0
    assert sum(b.values()) == 18000


def test_budget_all_five_purposes_conserve_budget():
    """For every purpose, sum of allocations <= 90% of max_tokens
    (integer-division leftover never exceeds the number of non-off layers)."""
    max_t = 100_000
    overhead = int(max_t * 0.10)
    remaining = max_t - overhead
    for purpose in PURPOSE_WEIGHTS:
        b = compute_layer_budgets(purpose, max_t)
        total = sum(b.values())
        # Allow <=6 tokens of integer-division loss across 6 layers
        assert remaining - 6 <= total <= remaining, (
            f"{purpose}: total {total} not within {remaining}±6"
        )
        # Every layer weight "off" must be 0 tokens
        for layer, w in PURPOSE_WEIGHTS[purpose].items():
            if w == "off":
                assert b[layer] == 0


def test_budget_proportional_to_weight_units():
    """Sanity: within a purpose, ratios match WEIGHT_UNITS."""
    b = compute_layer_budgets("create_s2t", 50_000)
    # ontology (HEAVY=40) vs archived (light=10) → 4x
    assert b["ontology"] == 4 * b["archived"]
    # static (heavy=25) vs archived (light=10) → 2.5x
    assert b["static"] * 10 == b["archived"] * 25


def test_budget_unknown_purpose_raises():
    with pytest.raises(ValueError):
        compute_layer_budgets("fake_purpose", 10_000)


# =========================================================================
# §3f fingerprint stability
# =========================================================================

def test_fingerprint_same_inputs_same_hash():
    f1 = compute_fingerprint(["ekpo"], "eda_sql_generation", 20_000)
    f2 = compute_fingerprint(["ekpo"], "eda_sql_generation", 20_000)
    assert f1 == f2
    assert len(f1) == 16
    assert all(c in "0123456789abcdef" for c in f1)


def test_fingerprint_different_scope_differs():
    f1 = compute_fingerprint(["ekpo"], "eda_sql_generation", 20_000)
    f2 = compute_fingerprint(["ekko"], "eda_sql_generation", 20_000)
    assert f1 != f2


def test_fingerprint_different_purpose_differs():
    f1 = compute_fingerprint(["ekpo"], "eda_sql_generation", 20_000)
    f2 = compute_fingerprint(["ekpo"], "eda_classification", 20_000)
    assert f1 != f2


def test_fingerprint_order_independent_for_scope():
    """Design §3f uses sorted(scope_tables) inside the hash."""
    f1 = compute_fingerprint(["ekpo", "ekko"], "create_s2t", 50_000)
    f2 = compute_fingerprint(["ekko", "ekpo"], "create_s2t", 50_000)
    assert f1 == f2


# =========================================================================
# §3e scope resolution — S1, S2, S3 with fixture data
# =========================================================================

@pytest.fixture
def scope_fixture_db(tmp_path):
    """Minimal DuckDB with raw_sap tables + seeds sized to exercise cascade."""
    db = tmp_path / "scope_test.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    for t in ("ekpo", "ekko", "mseg", "equi", "lfa1"):
        conn.execute(f"CREATE TABLE raw_sap.{t} (col VARCHAR)")
    conn.execute("""
        CREATE TABLE main_seeds.analysis_findings (
            business_term_id VARCHAR, tables_explored VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.s2t_mapping (
            business_term_id VARCHAR, source_table VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.business_term_analysis_results (
            business_term_id VARCHAR, source_tables VARCHAR,
            executed_at_utc TIMESTAMP
        )
    """)
    yield conn, db
    conn.close()


def test_scope_s1_from_analysis_findings(scope_fixture_db):
    conn, _ = scope_fixture_db
    conn.execute(
        "INSERT INTO main_seeds.analysis_findings VALUES ('BG001', 'ekko,ekpo')"
    )
    res = resolve_scope(conn, term_id="BG001", scope_tables=None)
    assert res["strategy_used"] == "s1"
    assert sorted(res["resolved_tables"]) == ["ekko", "ekpo"]


def test_scope_s2_when_s1_empty(scope_fixture_db):
    conn, _ = scope_fixture_db
    # No analysis_findings for BG002, but s2t_mapping exists
    conn.execute(
        "INSERT INTO main_seeds.s2t_mapping VALUES ('BG002', 'mseg')"
    )
    res = resolve_scope(conn, term_id="BG002", scope_tables=None)
    assert res["strategy_used"] == "s2"
    assert res["resolved_tables"] == ["mseg"]


def test_scope_s3_when_s1_s2_empty(scope_fixture_db):
    conn, _ = scope_fixture_db
    conn.execute(
        "INSERT INTO main_seeds.business_term_analysis_results "
        "VALUES ('BG003', 'equi', CURRENT_TIMESTAMP)"
    )
    res = resolve_scope(conn, term_id="BG003", scope_tables=None)
    assert res["strategy_used"] == "s3"
    assert res["resolved_tables"] == ["equi"]


def test_scope_s5_fallback_when_nothing_matches(scope_fixture_db):
    conn, _ = scope_fixture_db
    res = resolve_scope(conn, term_id="BG999_nonexistent", scope_tables=None)
    assert res["strategy_used"] == "s5"
    assert res["resolved_tables"] == []


def test_scope_explicit_overrides_everything(scope_fixture_db):
    conn, _ = scope_fixture_db
    conn.execute(
        "INSERT INTO main_seeds.analysis_findings VALUES ('BG004', 'ekko')"
    )
    res = resolve_scope(conn, term_id="BG004", scope_tables=["ekpo", "lfa1"])
    assert res["strategy_used"] == "explicit"
    assert sorted(res["resolved_tables"]) == ["ekpo", "lfa1"]


def test_scope_explicit_filters_nonexistent_tables(scope_fixture_db):
    conn, _ = scope_fixture_db
    # "fake_table" doesn't exist in raw_sap → excluded per validation check
    res = resolve_scope(conn, term_id=None,
                        scope_tables=["ekpo", "fake_table"])
    assert res["strategy_used"] == "explicit"
    assert res["resolved_tables"] == ["ekpo"]


def test_scope_neither_raises(scope_fixture_db):
    conn, _ = scope_fixture_db
    with pytest.raises(ContextScopeError):
        resolve_scope(conn, term_id=None, scope_tables=None)


def test_strategy_4_is_not_implemented():
    """Gate A stub: S4 must raise NotImplementedError."""
    with pytest.raises(NotImplementedError):
        ca._strategy_4(None, "BG001")


# =========================================================================
# §3j per-layer empty definitions
# =========================================================================

def test_layer_is_empty_static_all_zeros():
    assert _layer_is_empty(
        {"sap_data_dictionary": 0, "source_column_roles": 0,
         "movement_type_mapping": 0, "z_tables_catalog": 0,
         "information_schema": 0},
        "static",
    )


def test_layer_is_empty_static_with_info_schema():
    """3-source fallback: non-empty iff any of 3 static sources has rows."""
    assert not _layer_is_empty(
        {"sap_data_dictionary": 0, "source_column_roles": 0,
         "movement_type_mapping": 0, "z_tables_catalog": 0,
         "information_schema": 5},
        "static",
    )
    assert not _layer_is_empty(
        {"sap_data_dictionary": 10, "source_column_roles": 0,
         "information_schema": 0},
        "static",
    )


def test_layer_is_empty_dynamic_independent_sources():
    assert _layer_is_empty(
        {"domain_analysis_results": 0, "business_term_analysis_results": 0,
         "analysis_findings": 0},
        "dynamic",
    )
    assert not _layer_is_empty(
        {"domain_analysis_results": 0, "business_term_analysis_results": 0,
         "analysis_findings": 3},
        "dynamic",
    )


# =========================================================================
# Strict mode — §3j
# =========================================================================

def test_strict_raises_on_empty_heavy_static(tmp_path, monkeypatch):
    """When sap_data_dictionary + source_column_roles + info_schema are all
    empty for the scope, static layer is empty. eda_classification has
    static=HEAVY → strict raises ContextDegradedError."""
    empty_db = tmp_path / "empty.duckdb"
    conn = duckdb.connect(str(empty_db))
    conn.execute("CREATE SCHEMA raw_sap")  # no tables
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.sap_data_dictionary (
            table_name VARCHAR, field_name VARCHAR, data_type VARCHAR,
            description_en VARCHAR, business_meaning VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.source_column_roles (
            table_name VARCHAR, column_name VARCHAR, role VARCHAR,
            role_confidence VARCHAR, role_rationale VARCHAR
        )
    """)
    conn.close()

    monkeypatch.setattr(ca, "DB_PATH", empty_db)
    with pytest.raises(ContextDegradedError) as exc:
        assemble_context(
            purpose="eda_classification",
            scope_tables=["ekpo"],  # won't exist → dropped to []
            max_tokens=10_000,
            strict=True,
        )
    assert exc.value.layer == "static"


def test_strict_false_does_not_raise_on_empty_heavy(tmp_path, monkeypatch):
    """strict=False: empty HEAVY warns but bundle proceeds."""
    empty_db = tmp_path / "empty.duckdb"
    conn = duckdb.connect(str(empty_db))
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.sap_data_dictionary (
            table_name VARCHAR, field_name VARCHAR, data_type VARCHAR,
            description_en VARCHAR, business_meaning VARCHAR
        )
    """)
    conn.close()

    monkeypatch.setattr(ca, "DB_PATH", empty_db)
    bundle = assemble_context(
        purpose="eda_classification",
        scope_tables=["ekpo"],
        max_tokens=10_000,
        strict=False,
        include_debug_metadata=True,
    )
    assert bundle.token_count >= 0
    # Debug should flag the empty HEAVY layer
    assert "_warning" in bundle.debug["layer_details"]["static"]


def test_non_heavy_empty_does_not_raise(tmp_path, monkeypatch):
    """An empty heavy/light layer must not raise even in strict mode.

    Fresh fixture DB: static (HEAVY) loads via info_schema OK, but
    archived (light for create_s2t) finds nothing because archive_log is
    empty. Must not raise.
    """
    db = tmp_path / "nonheavy.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SCHEMA raw_sap")
    conn.execute("CREATE TABLE raw_sap.ekpo (EBELN VARCHAR)")
    conn.execute("CREATE SCHEMA main_seeds")
    for seed_sql in (
        "CREATE TABLE main_seeds.sap_data_dictionary "
        "(table_name VARCHAR, field_name VARCHAR, data_type VARCHAR, "
        "description_en VARCHAR, business_meaning VARCHAR)",
        "CREATE TABLE main_seeds.source_column_roles "
        "(table_name VARCHAR, column_name VARCHAR, role VARCHAR, "
        "role_confidence VARCHAR, role_rationale VARCHAR)",
        "CREATE TABLE main_seeds.archive_log "
        "(archive_id VARCHAR, term_name VARCHAR, archived_reason_code VARCHAR, "
        "archived_reason_text VARCHAR, archived_at_utc VARCHAR, "
        "learning_signal VARCHAR)",
        # Empty OBTs / supporting tables needed by loaders
        "CREATE TABLE main_seeds.analysis_findings "
        "(business_term_id VARCHAR, finding_type VARCHAR, query_description VARCHAR, "
        "result_summary VARCHAR, tables_explored VARCHAR)",
        "CREATE TABLE main_seeds.s2t_mapping "
        "(business_term_id VARCHAR, source_table VARCHAR, source_field VARCHAR, "
        "target_model VARCHAR, target_column VARCHAR)",
        "CREATE TABLE main_seeds.dbt_column_lineage "
        "(model_name VARCHAR, layer VARCHAR, column_name VARCHAR, "
        "origin_table VARCHAR, origin_column VARCHAR, transformation_type VARCHAR)",
        "CREATE TABLE main_seeds.business_glossary "
        "(id VARCHAR, term_name VARCHAR, display_name VARCHAR, definition VARCHAR, "
        "grain VARCHAR, unit VARCHAR, domain VARCHAR)",
        "CREATE TABLE main_seeds.domain_facts "
        "(category VARCHAR, scope_tables VARCHAR, fact_technical VARCHAR, "
        "status VARCHAR, auto_inject BOOLEAN)",
        "CREATE TABLE main_seeds.abap_logic_catalog "
        "(program_name VARCHAR, description VARCHAR, tables_read VARCHAR, "
        "tables_written VARCHAR, business_rule_plain VARCHAR, risk_level VARCHAR)",
        "CREATE TABLE main_seeds.movement_type_mapping "
        "(bwart VARCHAR, description_en VARCHAR, category VARCHAR, process_step VARCHAR)",
        "CREATE TABLE main_seeds.z_tables_catalog "
        "(table_name VARCHAR, description VARCHAR, business_purpose VARCHAR)",
    ):
        conn.execute(seed_sql)
    # Seed one info_schema-style column so static is non-empty
    # (static uses information_schema as fallback source)
    # raw_sap.ekpo.EBELN already gives info_schema 1 row
    conn.close()

    monkeypatch.setattr(ca, "DB_PATH", db)
    # eda_classification: static=HEAVY (must be non-empty — ekpo in raw_sap
    # gives info_schema row), archived=off, examples=heavy (abap empty).
    # heavy-empty must not raise in strict mode.
    bundle = assemble_context(
        purpose="eda_classification",
        scope_tables=["ekpo"],
        max_tokens=10_000,
        strict=True,
    )
    assert bundle.token_count >= 0
    assert bundle.scope_resolution["strategy_used"] == "explicit"


# ─── Stage D.1 — _dereference_cited_tars archive cascade ──────────────

def test_dereference_cited_tars_excludes_archived_source_term():
    """Stage D.1 (§28.11.8) — citation dereferencing must NOT surface
    TARs from archived source terms. Strict archive cascade per
    decision #67."""
    import duckdb as _ddb

    conn = _ddb.connect(":memory:")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.business_glossary (
            id VARCHAR, status VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.term_analysis_results (
            id VARCHAR, term_id VARCHAR, row_type VARCHAR,
            analysis_lens VARCHAR, stage VARCHAR, query_index INTEGER,
            query_sql VARCHAR, query_result_json VARCHAR,
            result_row_count INTEGER, interpretation VARCHAR,
            grounded_in_tar_ids VARCHAR, status VARCHAR,
            executed_at_utc TIMESTAMP
        )
    """)
    # Active source term with a cited TAR (should surface).
    conn.execute(
        "INSERT INTO main_seeds.business_glossary VALUES ('BG-ACTIVE', ?)",
        ["scope_confirmed"],
    )
    conn.execute("""
        INSERT INTO main_seeds.term_analysis_results VALUES (
            'TAR-00001', 'BG-ACTIVE', 'query', 'measures_overview',
            'framework_floor', 1, 'SELECT 1', '[]', 0, 'interp',
            '[]', 'success', '2026-04-23 10:00:00'
        )
    """)
    # Archived source term with a cited TAR (must be filtered out).
    conn.execute(
        "INSERT INTO main_seeds.business_glossary VALUES ('BG-ARCHIVED', ?)",
        ["archived"],
    )
    conn.execute("""
        INSERT INTO main_seeds.term_analysis_results VALUES (
            'TAR-00042', 'BG-ARCHIVED', 'query', 'measures_overview',
            'framework_floor', 1, 'SELECT 2', '[]', 0, 'interp',
            '[]', 'success', '2026-04-23 10:00:00'
        )
    """)

    citing_rows = [{
        "id": "TAR-99999",
        "grounded_in_tar_ids": '["TAR-00001", "TAR-00042"]',
    }]
    cited = ca._dereference_cited_tars(conn, citing_rows)
    cited_ids = {r["id"] for r in cited}
    assert "TAR-00001" in cited_ids, "active-term citation dropped"
    assert "TAR-00042" not in cited_ids, (
        "archived-term citation leaked — strict cascade violated"
    )


# =========================================================================
# known_issue #74 — _load_ontology dbt_column_lineage filter triple-pattern
# =========================================================================

def test_create_s2t_bundle_includes_both_layer_a_and_ontology_for_same_table():
    """Regression for known_issue #79.

    Piece 8's context_assembler must load BOTH the semantic_model
    (Layer A) AND dbt_column_lineage (ontology) layers when both
    have rows for the same scope table. Prior to #79 fix, §22.2
    prevented Layer A rows from ever being written for ontology-
    covered tables, so the scenario "both layers present for same
    table" never occurred in production. Post-#79, it does.

    This test verifies there is no cross-layer suppression in the
    assembly code — a Layer A row for `equi` AND an ontology row
    for raw_sap.equi both appear in the bundle with their own
    section headers.
    """
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA main_seeds")

    # Layer A row for equi.
    conn.execute("""
        CREATE TABLE main_seeds.semantic_model (
            table_name VARCHAR, canonical_alias VARCHAR,
            entity_class VARCHAR, primary_key_cols VARCHAR,
            typical_join_keys_json VARCHAR, code_column_refs_json VARCHAR,
            typical_filters VARCHAR, common_traps VARCHAR,
            reference_sql VARCHAR, source_dar_ids VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO main_seeds.semantic_model VALUES
          ('equi', 'eq', 'dim', 'EQUNR', '{}', '{}',
           'Filter active equipment', 'SERGE may differ from EQUNR',
           'SELECT ...', 'DAR-00100')
    """)

    # Ontology rows for same table (raw_sap.equi origin).
    conn.execute("""
        CREATE TABLE main_seeds.dbt_column_lineage (
            model_name VARCHAR, layer VARCHAR, column_name VARCHAR,
            origin_table VARCHAR, origin_column VARCHAR,
            transformation_type VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO main_seeds.dbt_column_lineage VALUES
          ('stg_sap__equi', 'staging', 'EQUNR', 'raw_sap.equi', 'EQUNR', 'direct')
    """)

    # Call the two independent loaders with scope=['equi']; each must
    # return its rows without suppressing the other.
    static_content, _, static_details = ca._load_static(
        conn, ["equi"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    ont_content, _, ont_details = ca._load_ontology(
        conn, ["equi"], term_id=None, budget=20_000,
    )

    assert static_details["semantic_model"] == 1, (
        f"expected 1 Layer A row for equi; got {static_details}"
    )
    assert ont_details["dbt_column_lineage"] == 1, (
        f"expected 1 ontology row for raw_sap.equi; got {ont_details}"
    )
    # Both rendered in their respective layers' content strings.
    assert "Semantic Model (Layer A)" in static_content
    assert "dbt_column_lineage" in ont_content


def test_load_ontology_matches_raw_sap_qualified_origin_table():
    """Regression test for known_issue #74.

    Prior to the fix, _load_ontology filtered dbt_column_lineage by
    `WHERE LOWER(origin_table) IN (scope)` with scope passing bare
    lowercase table names. But the seed stores origin_table in the
    dominant 'raw_sap.<table>' form, so every row was silently dropped
    for ontology-covered tables. This test asserts the triple-pattern
    fix returns rows when origin_table is 'raw_sap.equi' and scope
    passes bare 'equi'.
    """
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.dbt_column_lineage (
            model_name VARCHAR,
            layer VARCHAR,
            column_name VARCHAR,
            origin_table VARCHAR,
            origin_column VARCHAR,
            transformation_type VARCHAR
        )
    """)
    # raw_sap.<t> form — the dominant real-world storage pattern.
    conn.execute("""
        INSERT INTO main_seeds.dbt_column_lineage VALUES
            ('stg_sap__equi', 'staging', 'EQUNR', 'raw_sap.equi', 'EQUNR', 'direct'),
            ('hub_equipment', 'vault', 'hk_equipment', 'raw_sap.equi', 'EQUNR', 'hash_key'),
            ('sat_equipment_general', 'vault', 'MATNR', 'raw_sap.equi', 'MATNR', 'direct')
    """)
    # bare <t> form — also supported for backwards compatibility.
    conn.execute("""
        INSERT INTO main_seeds.dbt_column_lineage VALUES
            ('stg_sap__mseg', 'staging', 'BWART', 'mseg', 'BWART', 'direct')
    """)
    # unrelated row that must NOT match BG027 scope.
    conn.execute("""
        INSERT INTO main_seeds.dbt_column_lineage VALUES
            ('stg_sap__lfa1', 'staging', 'LIFNR', 'raw_sap.lfa1', 'LIFNR', 'direct')
    """)
    # s2t_mapping + raw_sap tables — required by _load_ontology but irrelevant here.
    conn.execute("""
        CREATE TABLE main_seeds.s2t_mapping (
            business_term_id VARCHAR, source_table VARCHAR,
            source_field VARCHAR, target_model VARCHAR, target_column VARCHAR
        )
    """)
    conn.execute("""
        CREATE SCHEMA raw_sap;
        CREATE TABLE raw_sap.equi (x INTEGER);
        CREATE TABLE raw_sap.mseg (x INTEGER);
    """)

    _content, _tokens, details = ca._load_ontology(
        conn, scope=["equi", "mseg"], term_id=None, budget=20_000,
    )

    # Pre-fix: this was 0 because 'raw_sap.equi' didn't match IN ('equi',...).
    # Post-fix: the triple-pattern matches all 3 raw_sap.equi rows + 1 bare
    # mseg row = 4.
    assert details["dbt_column_lineage"] == 4, (
        f"expected 4 rows (3 raw_sap.equi + 1 bare mseg); "
        f"got {details['dbt_column_lineage']}"
    )
    assert "stg_sap__equi" in _content
    assert "hub_equipment" in _content
    assert "sat_equipment_general" in _content
    assert "stg_sap__mseg" in _content
    # lfa1 row is out-of-scope and must not leak.
    assert "stg_sap__lfa1" not in _content


# =========================================================================
# Theme 1 C1 — DAR status surfacing + skipped-DAR consumption directive
# (sub-item 4 of known_issue #69; decision #73)
# =========================================================================

def _make_dar_db(rows: list[dict]):
    """In-memory DuckDB with main_seeds.domain_analysis_results populated.
    Each row dict needs id, analysis_type, source_tables, status,
    result_json. superseded_by + executed_at_utc default sensibly so the
    rows pass the loader's WHERE / ORDER BY clauses.
    """
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, analysis_type VARCHAR, source_tables VARCHAR,
            status VARCHAR, result_json VARCHAR, superseded_by VARCHAR,
            executed_at_utc TIMESTAMP
        )
    """)
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO main_seeds.domain_analysis_results "
            "(id, analysis_type, source_tables, status, result_json, "
            "superseded_by, executed_at_utc) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                r["id"], r["analysis_type"], r["source_tables"],
                r["status"], r["result_json"],
                r.get("superseded_by", ""),
                r.get("executed_at_utc", f"2026-04-{20 + i:02d} 10:00:00"),
            ],
        )
    return conn


def test_dar_loader_includes_status_column():
    """C1 T1 — verify the DAR SELECT projects `status` so the renderer
    can branch on it. Pre-C1 the SELECT was 4 cols (no status), so a
    skipped DAR rendered as an undifferentiated CSV row."""
    conn = _make_dar_db([{
        "id": "DAR-99001",
        "analysis_type": "temporal_coverage",
        "source_tables": "mard",
        "status": "skipped",
        "result_json": '{"skip_reason": "no date/timestamp columns in table"}',
    }])
    content, _toks, details = ca._load_dynamic(
        conn, scope=["mard"], term_id=None, budget=20_000,
    )
    assert details["domain_analysis_results"] == 1
    # If `status` weren't selected, the renderer couldn't emit STATUS=SKIPPED.
    assert "STATUS=SKIPPED" in content


def test_skipped_dar_rendering_carries_status_header():
    """C1 T2 — full skipped-DAR rendering: STATUS prefix + skip_reason
    line + adjacent success row stays clean (regression guard against
    noise on the 207-success-row dominant path)."""
    conn = _make_dar_db([
        {
            "id": "DAR-99001",
            "analysis_type": "completeness",
            "source_tables": "mard",
            "status": "success",
            "result_json": '{"col": "MATNR", "null_pct": 0.0}',
            "executed_at_utc": "2026-04-25 10:00:00",
        },
        {
            "id": "DAR-99002",
            "analysis_type": "temporal_coverage",
            "source_tables": "mard",
            "status": "skipped",
            "result_json": (
                '{"skip_reason": "no date/timestamp columns in table", '
                '"blockers_addressed": []}'
            ),
            "executed_at_utc": "2026-04-24 10:00:00",
        },
    ])
    content, _toks, details = ca._load_dynamic(
        conn, scope=["mard"], term_id=None, budget=20_000,
    )
    assert details["domain_analysis_results"] == 2
    assert "STATUS=SKIPPED — analyzer could not apply." in content
    assert "skip_reason: no date/timestamp columns in table" in content
    # Regression guard: the success row (DAR-99001) must NOT have STATUS=
    # in the two lines preceding its CSV row.
    lines = content.split("\n")
    success_idx = next(
        i for i, line in enumerate(lines) if line.startswith("DAR-99001,")
    )
    preceding = "\n".join(lines[max(0, success_idx - 2):success_idx])
    assert "STATUS=" not in preceding, (
        f"success row carries STATUS prefix unexpectedly: {preceding!r}"
    )


def test_skipped_dar_with_missing_skip_reason():
    """C1 T3 — defensive: skipped DAR with no skip_reason key falls back
    to '(not provided)'. _skipped_dar.py always populates skip_reason
    today; this guards against future drift."""
    conn = _make_dar_db([{
        "id": "DAR-99003",
        "analysis_type": "magnitude",
        "source_tables": "mard",
        "status": "skipped",
        # No skip_reason key — only blockers_addressed.
        "result_json": '{"blockers_addressed": []}',
    }])
    content, _toks, _details = ca._load_dynamic(
        conn, scope=["mard"], term_id=None, budget=20_000,
    )
    assert "STATUS=SKIPPED" in content
    assert "skip_reason: (not provided)" in content


def test_error_dar_rendering_carries_status_header():
    """C1 T4 — STATUS=ERROR rendering for analyzer-exception rows. C1
    surfaces error semantics in the renderer for status uniformity even
    though the iteration prompt doesn't teach error consumption (that's
    out of scope per the brief)."""
    conn = _make_dar_db([{
        "id": "DAR-99004",
        "analysis_type": "code_tables",
        "source_tables": "mseg",
        "status": "error",
        "result_json": '{"error": "join key collision"}',
    }])
    content, _toks, _details = ca._load_dynamic(
        conn, scope=["mseg"], term_id=None, budget=20_000,
    )
    assert "STATUS=ERROR — analyzer raised an exception." in content
    assert "(see result_json for trace)" in content


# =========================================================================
# #93 — filter join_cardinality from generic DAR dump
# =========================================================================

def _extract_generic_dar_section(content: str) -> str:
    """Slice out the `## domain_analysis_results` section from a bundle.
    Returns empty string if the section is absent. Used by #93 tests to
    distinguish the generic dump from the dedicated cardinality block.
    """
    marker = "## domain_analysis_results"
    if marker not in content:
        return ""
    after = content.split(marker, 1)[1]
    # Stop at the next `## ` heading.
    if "\n## " in after:
        after = after.split("\n## ", 1)[0]
    return after


def test_load_dynamic_excludes_cardinality_dars():
    """#93 T1 — cardinality DARs are filtered from the generic DAR dump
    for purpose='create_s2t' / 'pre_s2t_reasoning'. EDA-analyzer DARs
    that were previously crowded out by the LIMIT saturation now reach
    the bundle.

    #95 refactor: 10 EDA fixture rows now use 10 distinct
    (analysis_type, source_tables) pairs so per-pair surfacing keeps all
    of them. Original (pre-#95) fixture used 10 (completeness, mseg)
    rows which would collapse to 1 under per-pair semantics."""
    rows = []
    # 60 cardinality DARs (would saturate LIMIT 50 pre-fix).
    for i in range(60):
        rows.append({
            "id": f"DAR-90{i:03d}",
            "analysis_type": "join_cardinality",
            "source_tables": "mseg,equi",
            "status": "success",
            "result_json": "{}",
            "executed_at_utc": f"2026-04-26 12:{i // 60:02d}:{i % 60:02d}",
        })
    # 10 EDA-analyzer DARs across 5 types × 2 tables = 10 distinct
    # (type, tables) partitions. Each row has its own partition under
    # #95's per-pair surfacing.
    eda_types = [
        "completeness", "dimensions", "magnitude", "code_tables",
        "temporal_coverage",
    ]
    eda_tables = ["mseg", "equi"]
    i = 0
    for typ in eda_types:
        for tbl in eda_tables:
            rows.append({
                "id": f"DAR-91{i:03d}",
                "analysis_type": typ,
                "source_tables": tbl,
                "status": "success",
                "result_json": '{"col": "MATNR", "null_pct": 0.0}',
                "executed_at_utc": f"2026-04-25 10:{i:02d}:00",
            })
            i += 1
    conn = _make_dar_db(rows)
    content, _t, details = ca._load_dynamic(
        conn, scope=["mseg", "equi"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    # Post-fix: only the 10 EDA rows match (cardinality filtered in SQL).
    assert details["domain_analysis_results"] == 10
    generic = _extract_generic_dar_section(content)
    # Cardinality DAR ids must NOT appear in the generic dump.
    for i in range(60):
        assert f"DAR-90{i:03d}" not in generic, (
            f"cardinality DAR-90{i:03d} leaked into generic dump "
            f"despite #93 filter"
        )
    # All 10 EDA DAR ids must now appear (no LIMIT crowd-out post-fix).
    for i in range(10):
        assert f"DAR-91{i:03d}" in generic, (
            f"EDA DAR-91{i:03d} missing — LIMIT crowd-out not resolved"
        )


def test_load_dynamic_keeps_cardinality_for_non_s2t_purposes():
    """#93 T1b — for purposes OTHER than create_s2t / pre_s2t_reasoning
    the dedicated cardinality block does NOT run (gate at L962). Filter
    must NOT apply for those purposes — otherwise cardinality evidence
    would disappear from the bundle entirely. Mirror-image of T1's
    gating decision."""
    rows = [{
        "id": "DAR-90999",
        "analysis_type": "join_cardinality",
        "source_tables": "mseg,equi",
        "status": "success",
        "result_json": "{}",
    }]
    conn = _make_dar_db(rows)
    # purpose='eda_sql_generation' has no dedicated cardinality block;
    # the generic dump must keep cardinality rows visible.
    content, _t, details = ca._load_dynamic(
        conn, scope=["mseg"], term_id=None, budget=20_000,
        purpose="eda_sql_generation",
    )
    assert details["domain_analysis_results"] == 1
    assert "DAR-90999" in content


def test_cardinality_dedicated_block_unchanged_by_filter():
    """#93 T2 — regression guard. The dedicated cardinality block
    (Direction F.1, _load_create_s2t_cardinality) still renders per-pair
    structured evidence for create_s2t purpose. #93's filter only
    affects the generic DAR dump, not the dedicated path which uses
    its own SELECT scoped to analysis_type='join_cardinality'.

    result_json needs the fields _render_join_cardinality_block parses:
    t1/t2 (pair identity), kind, key_columns_t1/_t2, fanout_class
    (per_record_key / header_detail / catastrophic_fanout / no_signal —
    drives the classifier at scope_derivation.py:476), avg_fanout,
    stddev_fanout, matched_keys_ratio. Without fanout_class the renderer
    silently drops the row as 'no informative cardinality evidence'."""
    conn = _make_dar_db([{
        "id": "DAR-92001",
        "analysis_type": "join_cardinality",
        "source_tables": "equi,mseg",
        "status": "success",
        "result_json": (
            '{"t1": "equi", "t2": "mseg", "kind": "direct", '
            '"bridge_via": null, "key_columns_t1": ["MATNR"], '
            '"key_columns_t2": ["MATNR"], "confidence": "high", '
            '"fanout_class": "header_detail", "avg_fanout": 1.5, '
            '"stddev_fanout": 0.5, "matched_keys_ratio": 1.0}'
        ),
    }])
    content, _t, details = ca._load_dynamic(
        conn, scope=["mseg", "equi"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    # Dedicated block should fire (gates on purpose + scope, both met).
    assert details["join_cardinality_rendered"] is True
    assert "## join_cardinality" in content
    # Per-pair evidence rendered in the dedicated block.
    assert "equi <-> mseg" in content
    # And the same DAR is filtered OUT of the generic dump (#93 active).
    generic = _extract_generic_dar_section(content)
    assert "DAR-92001" not in generic, (
        "cardinality DAR leaked into generic dump — #93 filter inactive"
    )


def test_skipped_dar_now_reaches_bundle_post_cardinality_filter():
    """#93 T3 — end-to-end: pre-#93, 50 cardinality DARs would have
    saturated LIMIT 50 and crowded out skipped/error EDA DARs entirely.
    Post-#93, the cardinality rows are filtered from the generic dump
    and skipped/error EDA DARs surface with C1's STATUS prefix.

    Validates that #93 unblocks C1's production effect: STATUS=SKIPPED
    and STATUS=ERROR rendering reach the LLM-visible bundle.

    Tests _load_dynamic directly (rather than assemble_context as the
    spec suggests) to keep the fixture lean — _load_dynamic is the
    code path assemble_context delegates to for the dynamic layer.

    #95 refactor: skipped + error fixture rows now use distinct
    (analysis_type, source_tables) pairs so per-pair surfacing keeps
    all of them. Original (pre-#95) fixture packed 14 same-pair skipped
    + 3 same-pair error rows which would collapse to 1 + 1 under
    per-pair semantics."""
    rows = []
    # 50 cardinality DARs in scope (would saturate LIMIT 50 pre-fix).
    for i in range(50):
        rows.append({
            "id": f"DAR-93{i:03d}",
            "analysis_type": "join_cardinality",
            "source_tables": "mseg,equi",
            "status": "success",
            "result_json": "{}",
            "executed_at_utc": f"2026-04-26 13:{i // 60:02d}:{i % 60:02d}",
        })
    # 14 skipped DARs across 7 types × 2 tables (mard + mkpf) = 14
    # distinct partitions. All in BG027 scope.
    skipped_types = [
        "completeness", "dimensions", "magnitude", "code_tables",
        "temporal_coverage", "segmentation_threshold", "schema_discovery",
    ]
    skipped_tables = ["mard", "mkpf"]
    i = 0
    for typ in skipped_types:
        for tbl in skipped_tables:
            rows.append({
                "id": f"DAR-94{i:03d}",
                "analysis_type": typ,
                "source_tables": tbl,
                "status": "skipped",
                "result_json": '{"skip_reason": "no applicable columns"}',
                "executed_at_utc": f"2026-04-25 10:{i:02d}:00",
            })
            i += 1
    # 3 error DARs across 3 distinct (type, tables) pairs in BG027 scope
    # (using equi/objk/mseg — disjoint from skipped tables to avoid
    # cross-status partition collisions).
    error_pairs = [
        ("dimensions", "equi"),
        ("magnitude", "objk"),
        ("code_tables", "mseg"),
    ]
    for i, (typ, tbl) in enumerate(error_pairs):
        rows.append({
            "id": f"DAR-95{i:03d}",
            "analysis_type": typ,
            "source_tables": tbl,
            "status": "error",
            "result_json": '{"error": "analyzer raised an exception"}',
            "executed_at_utc": f"2026-04-25 09:{i:02d}:00",
        })
    conn = _make_dar_db(rows)
    content, _t, details = ca._load_dynamic(
        conn, scope=["equi", "mseg", "mkpf", "objk", "mard"],
        term_id=None, budget=20_000, purpose="create_s2t",
    )
    # Pre-#93 this would have been 50 cardinality crowding out everything.
    # Post-#93 + #95: 17 EDA-analyzer DARs reach the bundle (each a
    # distinct (type, tables) partition).
    assert details["domain_analysis_results"] == 17
    # C1's STATUS prefix rendering must fire post-#93 (this is the
    # production-effect unlock #93 delivers for Theme 1 sub-item 4).
    assert content.count("STATUS=SKIPPED") == 14, (
        f"expected 14 STATUS=SKIPPED markers, "
        f"got {content.count('STATUS=SKIPPED')}"
    )
    assert content.count("STATUS=ERROR") == 3, (
        f"expected 3 STATUS=ERROR markers, "
        f"got {content.count('STATUS=ERROR')}"
    )
    # No cardinality DAR ids in the generic dump section.
    generic = _extract_generic_dar_section(content)
    for i in range(50):
        assert f"DAR-93{i:03d}" not in generic, (
            f"cardinality DAR-93{i:03d} leaked into generic dump"
        )


# =========================================================================
# #95 — per-(analysis_type, source_tables, col_name)-pair surfacing
# =========================================================================

def test_load_dynamic_collapses_duplicate_pairs_to_latest():
    """#95 T1 — three active DARs with identical (analysis_type,
    source_tables, col_name) collapse to the most recent under per-pair
    surfacing. Mirrors the residual #73 supersede-bug case where older
    runs weren't superseded — JSON-aware partition picks the newer
    without code special-casing."""
    rows = []
    for i, ts in enumerate([
        "2026-04-23 10:00:00",   # oldest
        "2026-04-24 10:00:00",
        "2026-04-25 10:00:00",   # latest — should be the survivor
    ]):
        rows.append({
            "id": f"DAR-9510{i}",
            "analysis_type": "completeness",
            "source_tables": "mseg",
            "status": "success",
            "result_json": '{"col": "MATNR", "null_pct": 0.0}',
            "executed_at_utc": ts,
        })
    conn = _make_dar_db(rows)
    content, _t, details = ca._load_dynamic(
        conn, scope=["mseg"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    # Per-pair: 3 rows for the same (completeness, mseg, '') partition
    # → 1 row surfaces, the most recent.
    assert details["domain_analysis_results"] == 1
    assert "DAR-95102" in content, (
        "latest DAR-95102 (2026-04-25) should be the surviving row"
    )
    assert "DAR-95100" not in content, (
        "oldest DAR-95100 (2026-04-23) should be collapsed"
    )
    assert "DAR-95101" not in content, (
        "middle DAR-95101 (2026-04-24) should be collapsed"
    )


def test_load_dynamic_preserves_perf_baseline_per_column():
    """#95 T2 — JSON-aware partition: performance_baseline emits one row
    per numeric column (col_name in result_json). The partition includes
    json_extract_string(result_json, '$.col_name'), so all column
    baselines surface even though they share (analysis_type, source_tables)."""
    cols = ["LABST", "MEINS", "CHARG"]
    rows = [
        {
            "id": f"DAR-9520{i}",
            "analysis_type": "performance_baseline",
            "source_tables": "mard",
            "status": "success",
            "result_json": json.dumps({
                "col_name": col, "min": 1, "max": 100, "avg": 50.5,
            }),
            # Identical timestamps — proves col_name (not recency) drives
            # the per-pair distinction for perf_baseline.
            "executed_at_utc": "2026-04-25 10:00:00",
        }
        for i, col in enumerate(cols)
    ]
    conn = _make_dar_db(rows)
    content, _t, details = ca._load_dynamic(
        conn, scope=["mard"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    assert details["domain_analysis_results"] == 3, (
        "all 3 perf_baseline rows should surface (JSON-aware partition)"
    )
    for i in range(3):
        assert f"DAR-9520{i}" in content, (
            f"DAR-9520{i} (col={cols[i]}) missing — JSON-aware "
            f"partition collapsed it incorrectly"
        )


def test_load_dynamic_recovers_starved_table_evidence():
    """#95 T3 — reproduces BG027's starvation scenario in fixture form.
    50 newer DARs across 4 tables (a/b/c/d) + 10 older DARs on table e
    (5 single-table + 5 paired). Pre-#95 with LIMIT 50, the 10 older
    rows were evicted by recency. Post-#95 with LIMIT 100 + per-pair
    surfacing, all 60 reach the bundle.

    All 60 rows have distinct (analysis_type, source_tables) pairs so
    per-pair surfacing keeps each one."""
    rows = []
    # 50 newer DARs across 4 tables × multiple types = 50 distinct pairs.
    # Use 5 types × 4 tables = 20 single-table pairs, + 5 types × 6
    # paired (tables a/b/c/d → 6 unordered pairs) = 30 paired pairs.
    # 20 + 30 = 50.
    types_5 = ["completeness", "dimensions", "magnitude", "code_tables",
               "schema_discovery"]
    tables_4 = ["a", "b", "c", "d"]
    paired_4 = [
        "a,b", "a,c", "a,d", "b,c", "b,d", "c,d",
    ]
    i = 0
    for typ in types_5:
        for tbl in tables_4:
            rows.append({
                "id": f"DAR-9530{i:02d}",
                "analysis_type": typ,
                "source_tables": tbl,
                "status": "success",
                "result_json": "{}",
                "executed_at_utc": f"2026-04-26 12:00:{i:02d}",
            })
            i += 1
    for typ in types_5:
        for pair in paired_4:
            rows.append({
                "id": f"DAR-9530{i:02d}",
                "analysis_type": typ,
                "source_tables": pair,
                "status": "success",
                "result_json": "{}",
                "executed_at_utc": f"2026-04-26 12:01:{i:02d}",
            })
            i += 1
    assert i == 50, f"expected 50 newer rows, generated {i}"
    # 10 older "starved table E" rows: 7 single-table + 3 paired pairs.
    starved_pairs = [
        ("completeness", "e"),
        ("dimensions", "e"),
        ("magnitude", "e"),
        ("code_tables", "e"),
        ("schema_discovery", "e"),
        ("temporal_coverage", "e"),
        ("segmentation_threshold", "e"),
        ("grain_relationship", "a,e"),
        ("grain_relationship", "b,e"),
        ("grain_relationship", "c,e"),
    ]
    for j, (typ, tbl) in enumerate(starved_pairs):
        rows.append({
            "id": f"DAR-9540{j:02d}",
            "analysis_type": typ,
            "source_tables": tbl,
            "status": "success",
            "result_json": "{}",
            "executed_at_utc": f"2026-04-25 09:00:{j:02d}",
        })
    conn = _make_dar_db(rows)
    content, _t, details = ca._load_dynamic(
        conn, scope=["a", "b", "c", "d", "e"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    # 60 distinct partitions in scope, LIMIT 100 → all surface.
    assert details["domain_analysis_results"] == 60, (
        f"expected all 60 distinct-pair rows to surface under LIMIT 100; "
        f"got {details['domain_analysis_results']}"
    )
    # Critical assertion: ALL 10 starved table-E rows reach the bundle.
    # This is the production-effect unlock #95 delivers.
    for j in range(10):
        assert f"DAR-9540{j:02d}" in content, (
            f"starved-table row DAR-9540{j:02d} missing — "
            f"#95 LIMIT raise didn't recover it"
        )


def test_load_dynamic_filters_scope_in_sql():
    """#95 T4 — scope filter pulled into SQL via list_intersect.
    Confirms the WHERE clause excludes out-of-scope rows so the Python
    post-filter pass is no longer needed."""
    rows = [
        # In scope (mseg overlaps)
        {
            "id": "DAR-95401",
            "analysis_type": "completeness",
            "source_tables": "mseg",
            "status": "success",
            "result_json": "{}",
        },
        # In scope (paired equi,mseg overlaps)
        {
            "id": "DAR-95402",
            "analysis_type": "dimensions",
            "source_tables": "equi,mseg",
            "status": "success",
            "result_json": "{}",
        },
        # OUT of scope (lfa1, mara — neither in scope)
        {
            "id": "DAR-95403",
            "analysis_type": "magnitude",
            "source_tables": "lfa1",
            "status": "success",
            "result_json": "{}",
        },
        # OUT of scope (paired but neither side in scope)
        {
            "id": "DAR-95404",
            "analysis_type": "code_tables",
            "source_tables": "lfa1,mara",
            "status": "success",
            "result_json": "{}",
        },
    ]
    conn = _make_dar_db(rows)
    content, _t, details = ca._load_dynamic(
        conn, scope=["mseg", "equi"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    assert details["domain_analysis_results"] == 2, (
        f"expected 2 in-scope rows; got {details['domain_analysis_results']}"
    )
    assert "DAR-95401" in content, "in-scope mseg row missing"
    assert "DAR-95402" in content, "in-scope equi,mseg paired row missing"
    assert "DAR-95403" not in content, (
        "out-of-scope lfa1 row leaked — SQL scope filter inactive"
    )
    assert "DAR-95404" not in content, (
        "out-of-scope lfa1,mara paired row leaked"
    )


def test_schema_discovery_renders_in_compact_form():
    """#95 T5 (rewritten by fix-up) — schema_discovery DARs render in
    compact PK/FK/SHAPE/BRIDGE form rather than as the raw JSON blob.

    Fix-up history:
      - #95 (commit 819d3fe): initial compaction; SHAPE used wrong keys
        (kind / columns) — rendered empty SHAPE lines on every active
        DAR. bridge_tables had no branch — silently dropped.
      - This T5 rewrite: fixture uses live-data shape (DAR-00215 mkpf
        as reference: 1 PK, 1 FK, 1 shape, 21 bridges → bridge cap
        fires with footer for 16 additional). Original T5 was a
        false-positive (fixture used helper's wrong keys, not live
        keys), masking the bugs."""
    # Build a fixture matching DAR-00215's live-data shape: 1 PK,
    # 1 FK, 1 SHAPE, 21 BRIDGEs (so bridge cap fires).
    bridges = []
    bridge_targets = [
        "bseg", "eban", "ekbe", "eket", "ekkn", "ekko", "ekpo_a",
        "ekpo_b", "equi", "makt", "mara", "marc", "mard", "marm",
        "objk", "rbkp", "resb", "rseg_a", "rseg_b", "ser01", "ser03",
    ]  # 21 entries — matches DAR-00215's live count
    for tgt in bridge_targets:
        bridges.append({
            "between": ["mkpf", tgt],
            "via": "mseg",
            "path": (
                f"mkpf.MBLNR -> mseg.MBLNR && "
                f"mseg.MATNR -> {tgt}.MATNR"
            ),
            "confidence": "medium",
        })
    rj = json.dumps({
        "pk_candidates": [{
            "columns": ["MBLNR"],
            "confidence": 1.0,
            "distinct_ratio": 1.0,
            "null_count": 0,
            "evidence": "count(distinct MBLNR) = 31965, row_count = 31965, no nulls",
        }],
        "fk_candidates": [{
            "from_columns": ["MBLNR"],
            "to_table": "mseg",
            "to_columns": ["MBLNR"],
            "referential_integrity_pct": 100.0,
            "value_overlap_count": 31965,
            "confidence": "high",
            "evidence": "31965/31965 distinct values from mkpf.MBLNR exist in mseg.MBLNR",
        }],
        "relationship_shapes": [{
            "pair": ["mkpf", "mseg"],
            "via_columns": ["MBLNR"],
            "shape": "one_to_one",
            "cardinality": "1:1",
            "avg_children_per_parent": 1.0,
            "confidence": "high",
            "evidence": "mkpf has 31965 rows / 31965 distinct MBLNR; mseg has 31965 rows / 31965 distinct MBLNR. Cardinality ratio 1.0:1.",
        }],
        "bridge_tables": bridges,
    })
    rows = [{
        "id": "DAR-95500",
        "analysis_type": "schema_discovery",
        "source_tables": "mkpf",
        "status": "success",
        "result_json": rj,
    }]
    conn = _make_dar_db(rows)
    content, _t, _details = ca._load_dynamic(
        conn, scope=["mkpf"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    # PK + FK render correctly (unchanged from #95).
    assert "PK: MBLNR (confidence=1.0)" in content
    assert "FK: MBLNR -> mseg.MBLNR (RI=100.0%, confidence=high)" in content
    # SHAPE line renders with locked format (regression of #95 bug).
    assert (
        "SHAPE: one_to_one [1:1] mkpf↔mseg via MBLNR (confidence=high)"
        in content
    ), "SHAPE line missing or wrong format — #95 bug regressed"
    # No empty SHAPE lines (regression guard against the original #95 bug).
    assert "SHAPE:  on " not in content, (
        "empty 'SHAPE:  on ' line still rendered — #95 helper-keys bug"
    )
    # Bridge cap fired: exactly 5 BRIDGE lines + footer for 16 more.
    bridge_count = content.count("BRIDGE: ")
    assert bridge_count == 5, (
        f"expected 5 BRIDGE lines (cap), got {bridge_count}"
    )
    assert (
        "(16 additional bridges; see DAR-95500 for full list)" in content
    ), "bridge cap footer missing or wrong"
    # Verbose evidence prose dropped.
    assert "count(distinct" not in content
    assert "31965/31965" not in content
    assert "Cardinality ratio" not in content


def test_schema_discovery_renders_sum_match_when_present():
    """Fix-up — when relationship_shape carries sum_match_pct, render as
    rounded integer percentage in the SHAPE line."""
    rj = json.dumps({
        "relationship_shapes": [{
            "pair": ["mseg", "bseg"],
            "via_columns": ["MATNR"],
            "shape": "header_detail",
            "cardinality": "1:N",
            "avg_children_per_parent": 6393.0,
            "confidence": "high",
            "sum_match_pct": 0.9235,
            "sum_match_column": "DMBTR",
            "evidence": "...",
        }],
    })
    rows = [{
        "id": "DAR-95501",
        "analysis_type": "schema_discovery",
        "source_tables": "mseg",
        "status": "success",
        "result_json": rj,
    }]
    conn = _make_dar_db(rows)
    content, _t, _details = ca._load_dynamic(
        conn, scope=["mseg"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    # 0.9235 → "sum_match=92%" (rounded integer, not "0.9235" or "92.35%").
    assert "sum_match=92%" in content, (
        "sum_match should render as rounded integer percentage"
    )
    # Full SHAPE line is correct.
    assert (
        "SHAPE: header_detail [1:N] mseg↔bseg via MATNR "
        "(confidence=high, sum_match=92%)" in content
    )
    # Raw fraction not leaked.
    assert "0.9235" not in content


def test_schema_discovery_caps_apply_per_dar_not_globally():
    """Fix-up — caps fire per-DAR, not across DARs. Two DARs each with
    8 shapes should each render 5 + a footer for 3 additional, separately."""
    def make_shapes(table_a):
        return [
            {
                "pair": [table_a, f"t{i:02d}"],
                "via_columns": ["KEY"],
                "shape": "one_to_one",
                "cardinality": "1:1",
                "confidence": "high",
            }
            for i in range(8)
        ]

    rows = [
        {
            "id": "DAR-95510",
            "analysis_type": "schema_discovery",
            "source_tables": "alpha",
            "status": "success",
            "result_json": json.dumps({"relationship_shapes": make_shapes("alpha")}),
        },
        {
            "id": "DAR-95511",
            "analysis_type": "schema_discovery",
            "source_tables": "beta",
            "status": "success",
            "result_json": json.dumps({"relationship_shapes": make_shapes("beta")}),
        },
    ]
    conn = _make_dar_db(rows)
    content, _t, _details = ca._load_dynamic(
        conn, scope=["alpha", "beta"], term_id=None, budget=20_000,
        purpose="create_s2t",
    )
    # Each DAR renders 5 shapes — total 10 SHAPE lines.
    assert content.count("SHAPE: ") == 10, (
        f"expected 10 SHAPE lines (5 per DAR × 2 DARs), "
        f"got {content.count('SHAPE: ')}"
    )
    # Each DAR has its own cap-footer citing its own id.
    assert "(3 additional shapes; see DAR-95510 for full list)" in content
    assert "(3 additional shapes; see DAR-95511 for full list)" in content


def test_pre_s2t_reasoning_dynamic_budget_post_redistribution():
    """Fix-up — regression guard for the pre_s2t_reasoning budget
    redistribution. Demoting business HEAVY (40) → heavy (25) shifts
    weight units from 150 → 135. Resulting dynamic budget = 45000 ×
    40/135 = 13333. Without this redistribution, the corrected
    schema_discovery rendering doesn't fit in the dynamic layer for
    BG027-class scopes."""
    budgets = ca.compute_layer_budgets("pre_s2t_reasoning", 50_000)
    assert budgets["dynamic"] == 13333, (
        f"expected dynamic budget 13333 post-redistribution, "
        f"got {budgets['dynamic']} — has the business weight been "
        f"changed back to HEAVY?"
    )
    assert budgets["business"] == 8333, (
        f"expected business budget 8333 (heavy=25 units), "
        f"got {budgets['business']}"
    )
    # Other layers unchanged.
    assert budgets["static"] == 8333  # heavy=25 (unchanged share)
    assert budgets["ontology"] == 8333  # heavy=25 (unchanged share)
    assert budgets["examples"] == 3333  # light=10 (unchanged share)
    assert budgets["archived"] == 3333  # light=10 (unchanged share)
