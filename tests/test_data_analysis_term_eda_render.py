"""KI-108 — Data_Analysis.py Stage C render branch tests.

The render path (`_render_stage_c_post_run` in app/pages/Data_Analysis.py)
was filtering sufficiency rows on `status='success'` only, hiding
KI-102-class quarantined runs behind a 'no run yet' message. This
commit drops the status filter and adds a quarantine-aware banner.

Tests verify:
  - The widened SELECT picks up both status values.
  - The empty-table branch still returns nothing (existing behavior).
  - The quarantined render branch + KI-108 user-facing copy exist
    in source (streamlit page rendering tested via the demo walk;
    source-string verification mirrors test_bar_section_rendering.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
_DA_PY = _ROOT / "app" / "pages" / "Data_Analysis.py"


# Replicates the post-KI-108 SELECT verbatim. If the page changes the
# query, this test breaks loudly (which is what we want).
_SUFFICIENCY_SELECT_SQL = """
    SELECT id, sufficiency_json, confidence, executed_at_utc,
           run_id, status, validation_errors_json
    FROM main_seeds.term_analysis_results
    WHERE term_id = ? AND row_type = 'sufficiency'
      AND status IN ('success', 'quarantined')
    ORDER BY executed_at_utc DESC LIMIT 1
"""


def _setup_tar_db(tmp_path, rows):
    """Build a minimal DuckDB with the schema _render_stage_c_post_run
    queries. `rows` is a list of 9-tuples matching the INSERT below."""
    db = tmp_path / "cpe_analytics.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SCHEMA main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.term_analysis_results (
            id VARCHAR, term_id VARCHAR, row_type VARCHAR,
            sufficiency_json VARCHAR, confidence VARCHAR,
            executed_at_utc TIMESTAMP, run_id VARCHAR, status VARCHAR,
            validation_errors_json VARCHAR
        )
    """)
    conn.execute("""
        CREATE TABLE main_seeds.business_glossary (
            id VARCHAR, status VARCHAR
        )
    """)
    conn.execute(
        "INSERT INTO main_seeds.business_glossary VALUES "
        "('BG-T', 'scope_confirmed')"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO main_seeds.term_analysis_results "
            "(id, term_id, row_type, sufficiency_json, confidence, "
            " executed_at_utc, run_id, status, validation_errors_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            list(r),
        )
    return conn


def test_render_term_eda_with_success_sufficiency(tmp_path) -> None:
    """SELECT picks up a status='success' sufficiency row and exposes
    its fields in the order the page unpacks them."""
    conn = _setup_tar_db(tmp_path, [
        ("TAR-00001", "BG-T", "sufficiency",
         '{"declared_sufficient": true}', "high",
         "2026-04-29 11:00:00", "TARRUN-A", "success", None),
    ])
    try:
        row = conn.execute(_SUFFICIENCY_SELECT_SQL, ["BG-T"]).fetchone()
        assert row is not None
        (suff_id, suff_json, conf, exec_at, run_id, status, errs) = row
        assert suff_id == "TAR-00001"
        assert status == "success"
        assert errs is None
    finally:
        conn.close()


def test_render_term_eda_with_quarantined_sufficiency(tmp_path) -> None:
    """KI-108: quarantined sufficiency rows are returned (not filtered
    out), validation_errors_json is parseable, and the page source
    contains the quarantined render branch with user-facing copy."""
    errors_json = json.dumps({
        "error_type": "tar_id_mismatch",
        "cited_ids": ["TAR-00001"],
        "allocated_ids_this_run": ["TAR-00050", "TAR-00051"],
        "unresolved_ids": ["TAR-00001"],
    })
    conn = _setup_tar_db(tmp_path, [
        ("TAR-00050", "BG-T", "sufficiency",
         '{"declared_sufficient": true}', "low",
         "2026-04-29 12:00:00", "TARRUN-B", "quarantined", errors_json),
    ])
    try:
        row = conn.execute(_SUFFICIENCY_SELECT_SQL, ["BG-T"]).fetchone()
        assert row is not None
        (suff_id, suff_json, conf, exec_at, run_id, status, errs) = row
        assert suff_id == "TAR-00050"
        assert status == "quarantined"
        assert errs is not None
        parsed = json.loads(errs)
        assert parsed["error_type"] == "tar_id_mismatch"
        assert parsed["unresolved_ids"] == ["TAR-00001"]
    finally:
        conn.close()

    # Source-string verification: the page contains the quarantined
    # render branch and the KI-108 user-facing copy.
    src = _DA_PY.read_text(encoding="utf-8")
    assert "status IN ('success', 'quarantined')" in src
    assert '_suff_status == "quarantined"' in src
    assert "**quarantined**" in src
    assert "KI-102" in src and "KI-109" in src
    # Transition button gated to success-only (no transition allowed
    # while sufficiency verdict is quarantined).
    assert '_suff_status == "success"' in src


def test_render_term_eda_with_no_sufficiency_row(tmp_path) -> None:
    """Empty term_analysis_results → SELECT returns None → caller
    surfaces the existing 'no Term EDA run has completed yet' message."""
    conn = _setup_tar_db(tmp_path, [])
    try:
        row = conn.execute(_SUFFICIENCY_SELECT_SQL, ["BG-T"]).fetchone()
        assert row is None
    finally:
        conn.close()


def test_render_term_eda_with_malformed_validation_errors_json() -> None:
    """The render branch defensively catches JSONDecodeError when
    parsing both sufficiency_json and validation_errors_json (writer
    always emits valid JSON; defense-in-depth for historical rows or
    direct CSV edits)."""
    src = _DA_PY.read_text(encoding="utf-8")
    # Find the render function block boundaries.
    suff_idx = src.find("_render_stage_c_post_run")
    end_idx = src.find("def _live_raw_sap_tables", suff_idx)
    assert suff_idx > -1 and end_idx > suff_idx, (
        "could not locate _render_stage_c_post_run boundaries"
    )
    block = src[suff_idx:end_idx]
    # Both _json.loads sites (sufficiency_json + validation_errors_json)
    # are wrapped in their own JSONDecodeError catches.
    assert "_json.loads(_suff_json or" in block
    assert "_json.loads(_validation_errors_json or" in block
    assert block.count("_json.JSONDecodeError") >= 2
