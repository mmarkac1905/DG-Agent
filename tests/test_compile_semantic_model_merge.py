"""Stage F Commit 4 — unit tests for _merge_schema_discovery_fks in
scripts/compile_semantic_model.py. Verifies the Option (iv) lite
deterministic override pass applied after LLM synthesis.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import compile_semantic_model as mod  # noqa: E402


def _fresh_conn() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with main_seeds.domain_analysis_results primed."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.domain_analysis_results (
            id VARCHAR, analysis_type VARCHAR, executed_at_utc TIMESTAMP,
            result_json VARCHAR, status VARCHAR, source_tables VARCHAR
        )
    """)
    return conn


def test_01_no_schema_discovery_dar_leaves_emitted_unchanged() -> None:
    conn = _fresh_conn()
    # No schema_discovery DAR for mseg.
    emitted = {
        "typical_join_keys_json": json.dumps({"mara": ["MATNR"]}),
        "canonical_alias": "mseg",
    }
    result = mod._merge_schema_discovery_fks(emitted, "mseg", conn)
    # Unchanged emission — still flat-shape
    keys = json.loads(result["typical_join_keys_json"])
    assert keys == {"mara": ["MATNR"]}, f"expected unchanged flat shape, got {keys}"


def test_02_high_integrity_fks_override_llm() -> None:
    conn = _fresh_conn()
    sd_payload = {
        "fk_candidates": [
            {
                "from_columns": ["MATNR"],
                "to_table": "mara",
                "to_columns": ["MATNR"],
                "referential_integrity_pct": 100.0,
                "confidence": "high",
            },
            {
                "from_columns": ["EBELN"],
                "to_table": "ekko",
                "to_columns": ["EBELN"],
                "referential_integrity_pct": 99.0,
                "confidence": "high",
            },
        ],
    }
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results VALUES "
        "('DAR-00001', 'schema_discovery', '2026-04-23 10:00:00', ?, "
        "'success', 'mseg')",
        [json.dumps(sd_payload)],
    )
    emitted = {
        "typical_join_keys_json": json.dumps({"mara": ["OLD_COL"]}),
    }
    result = mod._merge_schema_discovery_fks(emitted, "mseg", conn)
    keys = json.loads(result["typical_join_keys_json"])
    # mara entry should be overridden by schema_discovery
    assert "mara" in keys
    assert keys["mara"]["source"] == "schema_discovery"
    assert keys["mara"]["columns"] == ["MATNR"]
    assert keys["mara"]["integrity_pct"] == 100.0
    # ekko entry should be inserted
    assert "ekko" in keys
    assert keys["ekko"]["source"] == "schema_discovery"
    assert keys["ekko"]["integrity_pct"] == 99.0


def test_03_medium_integrity_below_threshold_leaves_llm_output_intact() -> None:
    conn = _fresh_conn()
    sd_payload = {
        "fk_candidates": [
            {
                "from_columns": ["MATNR"],
                "to_table": "mara",
                "to_columns": ["MATNR"],
                "referential_integrity_pct": 85.0,  # below 95% threshold
                "confidence": "medium",
            },
        ],
    }
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results VALUES "
        "('DAR-00001', 'schema_discovery', '2026-04-23 10:00:00', ?, "
        "'success', 'mseg')",
        [json.dumps(sd_payload)],
    )
    emitted = {
        "typical_join_keys_json": json.dumps({"mara": ["LLM_COL"]}),
    }
    result = mod._merge_schema_discovery_fks(emitted, "mseg", conn)
    keys = json.loads(result["typical_join_keys_json"])
    # LLM output untouched — medium-confidence FK does NOT override
    assert keys == {"mara": ["LLM_COL"]}, (
        f"expected unchanged LLM output for below-threshold FK, got {keys}"
    )


def test_04_partial_overlap_preserves_llm_for_uncovered_targets() -> None:
    """LLM covers 3 targets; schema_discovery covers 2 at high confidence
    (1 overlaps). Final: LLM's 1 non-overlapping + schema_discovery's 2."""
    conn = _fresh_conn()
    sd_payload = {
        "fk_candidates": [
            {
                "from_columns": ["MATNR"],
                "to_table": "mara",
                "to_columns": ["MATNR"],
                "referential_integrity_pct": 100.0,
                "confidence": "high",
            },
            {
                "from_columns": ["EBELN"],
                "to_table": "ekko",
                "to_columns": ["EBELN"],
                "referential_integrity_pct": 100.0,
                "confidence": "high",
            },
        ],
    }
    conn.execute(
        "INSERT INTO main_seeds.domain_analysis_results VALUES "
        "('DAR-00001', 'schema_discovery', '2026-04-23 10:00:00', ?, "
        "'success', 'mseg')",
        [json.dumps(sd_payload)],
    )
    emitted = {
        "typical_join_keys_json": json.dumps({
            "mara": ["LLM_MATNR"],       # will be overridden by schema_discovery
            "lfa1": ["LIFNR"],           # NOT covered by schema_discovery → stays llm
        }),
    }
    result = mod._merge_schema_discovery_fks(emitted, "mseg", conn)
    keys = json.loads(result["typical_join_keys_json"])

    # All three should be present
    assert set(keys) == {"mara", "ekko", "lfa1"}
    # mara: schema_discovery overrode
    assert keys["mara"]["source"] == "schema_discovery"
    assert keys["mara"]["columns"] == ["MATNR"]
    # ekko: schema_discovery inserted
    assert keys["ekko"]["source"] == "schema_discovery"
    # lfa1: LLM preserved (migrated from flat to nested with llm_authored tag)
    assert keys["lfa1"]["source"] == "llm_authored"
    assert keys["lfa1"]["columns"] == ["LIFNR"]
    assert keys["lfa1"]["integrity_pct"] is None


# =========================================================================
# known_issue #79 — §22.2 ontology-skip gate removal
# =========================================================================

def test_has_ontology_coverage_function_removed_from_module() -> None:
    """Regression guard for #79 — the has_ontology_coverage function
    is the gate that conflated Layer A narrative with ontology lineage.
    It was removed from compile_semantic_model.py so the compile loop
    no longer skips ontology-covered tables. If a future refactor
    accidentally re-adds it (e.g., copy-paste from a prior branch),
    this test fails.
    """
    assert not hasattr(mod, "has_ontology_coverage"), (
        "has_ontology_coverage must stay removed (known_issue #79 — "
        "ontology coverage is no longer a compile skip condition; "
        "Layer A and ontology are independent context layers in "
        "Piece 8's bundle)"
    )


def test_auto_generated_rows_not_human_protected_so_they_refresh() -> None:
    """Regression guard for #79 — auto_generated rows must remain
    eligible for overwrite on recompile.

    The refactor removes only the ontology skip gate; the merge logic
    (final.update(preserved)) continues to overlay human-protected
    rows on top of freshly-compiled output, and auto_generated rows
    without human protection get refreshed every compile run. A
    stricter interpretation (skip-if-row-exists) would freeze
    auto_generated rows — this test documents the chosen
    interpretation (A) and guards against a drift to (B).
    """
    auto_row = {
        "table_name": "equi",
        "canonical_alias": "eq",
        "populated_by": "eda_compile",
        "review_state": "auto_generated",
    }
    assert mod.is_human_protected(auto_row) is False, (
        "auto_generated rows must NOT be human-protected so that "
        "recompile refreshes them; otherwise the final merge would "
        "skip overwrites and stale rows persist"
    )


def test_human_reviewed_and_override_rows_are_protected() -> None:
    """Regression for #79 — idempotency half: human-touched rows stay
    preserved across recompile regardless of ontology coverage or
    anything else. is_human_protected is the predicate the merge
    uses to decide which rows to preserve.
    """
    reviewed = {
        "table_name": "zmm_approval_log",
        "populated_by": "eda_compile",
        "review_state": "human_reviewed",
    }
    override = {
        "table_name": "zphase2_fixture",
        "populated_by": "human_override",
        "review_state": "auto_generated",
    }
    assert mod.is_human_protected(reviewed) is True
    assert mod.is_human_protected(override) is True


# =========================================================================
# known_issue #80 — conn threaded through to assemble_context
# =========================================================================

def test_assemble_bundle_accepts_conn_and_avoids_empty_bundle_warning(
    capfd,
) -> None:
    """Regression for #80.

    Prior to the fix, `_assemble_bundle` called `assemble_context()`
    without passing the parent's open connection. When compile's
    main() holds a read-write conn, DuckDB's in-process rule ("one
    conn per DB file per config") prevented assemble_context from
    opening its own read-only conn — it raised ConnectionError, the
    except block printed `WARNING: bundle assembly failed,
    proceeding with empty bundle: ...`, and LLM synthesis proceeded
    without DAR-grounded context.

    The fix threads the caller's conn through to assemble_context
    so the second conn is never opened. This test calls
    `_assemble_bundle` with a parent conn (simulating compile's
    main()) and asserts the degradation warning never prints.
    """
    # Use the production DuckDB — we need real seeds for assemble_context
    # to return anything meaningful. Uses an arbitrary ingested raw table
    # ('mseg' or similar is always present in the fixture catalog).
    from pathlib import Path
    db = Path(__file__).resolve().parent.parent / "cpe_analytics.duckdb"
    if not db.exists():
        import pytest
        pytest.skip("cpe_analytics.duckdb not available — env-specific test")

    # Open read-write conn (mirrors compile_semantic_model.main() at 719).
    parent_conn = duckdb.connect(str(db))
    try:
        # Drop-and-recover pattern from prod: pick any table that has
        # at least one current DAR. If none, skip.
        existing = parent_conn.execute("""
            SELECT DISTINCT LOWER(source_tables)
            FROM main_seeds.domain_analysis_results
            WHERE (superseded_by IS NULL OR superseded_by = '')
              AND source_tables NOT LIKE '%,%'
            LIMIT 1
        """).fetchone()
        if not existing:
            import pytest
            pytest.skip("no DAR rows in seed to exercise assemble_context")
        target_table = existing[0]

        capfd.readouterr()  # drain pre-existing captured output
        bundle = mod._assemble_bundle(
            term_ids=None,
            scope_tables=[target_table],
            conn=parent_conn,
        )
        captured = capfd.readouterr()
    finally:
        parent_conn.close()

    # Bundle must be non-empty (the fix's positive signal).
    assert bundle and bundle.strip(), (
        f"expected non-empty bundle when conn is threaded; got {bundle!r}"
    )
    # Degradation warning must NOT have printed.
    combined_out = captured.out + captured.err
    assert "empty bundle" not in combined_out, (
        f"bundle assembly warning printed despite conn being threaded "
        f"— fix didn't land correctly. captured: {combined_out!r}"
    )


# ─── harness ──────────────────────────────────────────────────────────

def _run_standalone() -> int:
    tests = [
        test_01_no_schema_discovery_dar_leaves_emitted_unchanged,
        test_02_high_integrity_fks_override_llm,
        test_03_medium_integrity_below_threshold_leaves_llm_output_intact,
        test_04_partial_overlap_preserves_llm_for_uncovered_targets,
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
