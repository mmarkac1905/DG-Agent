"""Tests for app/_data_catalog_helpers.

Covers the Table Detail selector fix: resolve_table_detail_default_index
must return the cached table's position when present, 0 when the cache
is missing, and 0 when the cache is stale (no longer in the ingested
list). Combined with the unconditional cache write in
app/pages/Data_Catalog.py's Table Detail branch, this guarantees the
selector updates the cache on every rerun — preventing the "stuck on
alphabetically-first table (BKPF)" regression.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))

from _data_catalog_helpers import (  # noqa: E402
    resolve_table_detail_default_index,
    format_batch_error_expander,
    has_semantic_model_row,
    parse_compile_skip_reason,
)

try:
    import duckdb  # noqa: E402
except ImportError:  # pragma: no cover
    duckdb = None


def test_selector_updates_cache_on_rerun() -> None:
    """One test, three scenarios — the contract that makes the fix work.

    After the fix, the Table Detail branch writes
    session_state['data_catalog_selected_table'] = widget_value on every
    rerun. That means on each subsequent rerun, `cached_table` equals
    whatever the widget currently shows, and default_index must land on
    that same option so the rendered view agrees with the cache.
    """
    tables = ["BKPF", "EBAN", "EKKO", "EKPO", "MARA"]

    # Scenario 1 — first render, no cache: selectbox defaults to index 0.
    assert resolve_table_detail_default_index(None, tables) == 0

    # Scenario 2 — rerun after user picked MARA: cache holds MARA,
    # default_index must resolve to MARA's position so the selectbox
    # re-renders on the same table (not snapping back to BKPF).
    assert resolve_table_detail_default_index("MARA", tables) == 4

    # Scenario 3 — stale cache (table no longer ingested): fall back to
    # 0 rather than raising ValueError from .index() lookup.
    assert resolve_table_detail_default_index("REMOVED_TABLE", tables) == 0


# =========================================================================
# known_issue #75 — batch wrapper surfaces per-analyzer stderr
# =========================================================================

def test_batch_error_expander_renders_stderr_for_each_failed_analyzer() -> None:
    """format_batch_error_expander produces an expander label + markdown
    body that contains each failed analyzer's stderr verbatim.

    Regression guard for the silent-failure batch dispatch bug: prior
    code counted errors but never surfaced stderr to the user. With the
    fix, a batch run on a fresh table that hits ContextDegradedError
    surfaces all 4 LLM analyzer stderrs in a single expander.
    """
    errors = [
        {
            "label": "completeness",
            "returncode": -2,
            "stderr": "ContextDegradedError: HEAVY layer 'dynamic' is empty.",
        },
        {
            "label": "dimensions",
            "returncode": -2,
            "stderr": "ContextDegradedError: HEAVY layer 'dynamic' is empty.",
        },
        {
            "label": "magnitude",
            "returncode": 1,
            "stderr": "retries exhausted\ntraceback (most recent...)",
        },
    ]
    out = format_batch_error_expander("objk", errors)
    assert out is not None, "expected an expander payload for non-empty errors"
    label, body = out

    # Label: table name + count + CTA.
    assert "`objk`" in label
    assert "3 analyzer errors" in label
    assert "click to view stderr" in label

    # Body: every analyzer's label + returncode + stderr must be present.
    assert "completeness" in body and "rc=-2" in body
    assert "dimensions" in body
    assert "magnitude" in body and "rc=1" in body
    assert "ContextDegradedError" in body
    assert "retries exhausted" in body


def test_batch_error_expander_returns_none_for_empty_errors() -> None:
    """When a table's inner loop produced no errors, helper returns None
    so the caller renders the ✓ 'done' line instead of an empty expander."""
    assert format_batch_error_expander("equi", []) is None


def test_batch_error_expander_handles_empty_stderr_gracefully() -> None:
    """A subprocess that crashed before writing to stderr still gets a
    row — body shows '(empty stderr)' rather than a blank code block."""
    out = format_batch_error_expander("mard", [
        {"label": "completeness", "returncode": -2, "stderr": ""},
    ])
    assert out is not None
    _label, body = out
    assert "(empty stderr)" in body


def test_batch_error_expander_singular_plural() -> None:
    """One error → 'error' (singular). Multiple → 'errors'."""
    single = format_batch_error_expander("t", [
        {"label": "x", "returncode": 1, "stderr": "e"},
    ])
    assert single is not None and "1 analyzer error " in single[0]
    assert "errors" not in single[0].split("error")[1][:5]  # no plural 's'


# =========================================================================
# known_issue #79 — has_semantic_model_row (replaces retired
# ontology_covers_table; Layer A panel branch now keys on row presence)
# =========================================================================

def _make_semantic_model_conn():
    """Fresh in-memory DuckDB with main_seeds.semantic_model schema."""
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE SCHEMA IF NOT EXISTS main_seeds")
    conn.execute("""
        CREATE TABLE main_seeds.semantic_model (
            table_name VARCHAR, canonical_alias VARCHAR, review_state VARCHAR
        )
    """)
    return conn


def test_has_semantic_model_row() -> None:
    """Regression for known_issue #79 — panel branch predicate.

    `has_semantic_model_row` decides whether the Layer A panel renders
    the existing row OR the 'run compile' empty state. True when any
    row exists for the requested table (case-insensitive); False on
    absence. Fails open (False) on query error — caller degrades to
    empty state rather than misreporting presence.
    """
    conn = _make_semantic_model_conn()
    conn.execute("""
        INSERT INTO main_seeds.semantic_model VALUES
          ('zmm_approval_log', 'zal', 'auto_generated'),
          ('zphase2_fixture', 'fx', 'human_reviewed')
    """)
    # Row exists → True (exact match).
    assert has_semantic_model_row(conn, "zmm_approval_log") is True
    # Case-insensitive on input.
    assert has_semantic_model_row(conn, "ZMM_APPROVAL_LOG") is True
    # Row exists regardless of review_state.
    assert has_semantic_model_row(conn, "zphase2_fixture") is True
    # No row → False.
    assert has_semantic_model_row(conn, "equi") is False


def test_has_semantic_model_row_fails_open_on_query_error() -> None:
    """Query error (seed missing, conn issue) → False so caller shows
    the 'run compile' empty state rather than pretending a row exists."""
    conn = duckdb.connect(":memory:")
    # No main_seeds.semantic_model → query raises → helper returns False.
    assert has_semantic_model_row(conn, "any_table") is False


def test_compile_toast_distinguishes_write_vs_skip() -> None:
    """Regression for known_issue #78.

    compile_semantic_model.py returns rc=0 whether it wrote a
    semantic_model row OR skipped the table (ontology / human_override
    / EDA-incomplete). The prior toast always read "Semantic model
    compiled for <t>" — a lie on the skip path. `parse_compile_skip_reason`
    is the parser that extracts the actual reason from stdout so the
    UI can distinguish and render honestly.

    Covers the three skip-message variants emitted by
    compile_semantic_model.py:651/656/663 plus a write-path stdout
    (no skip line for the target table → helper returns None).
    """
    # 1. Ontology-coverage skip — the BG027 case.
    stdout_onto = (
        "  compiled mseg — alias='m', ...\n"
        "  skip equi — ontology coverage exists in dbt_column_lineage\n"
        "  compiled zmm_approval_log — alias='zal', ...\n"
    )
    assert parse_compile_skip_reason(stdout_onto, "equi") == \
        "ontology coverage exists in dbt_column_lineage"

    # 2. human_override skip (preserved rows path at line 651).
    stdout_human = "  skip mara — human_override / human_reviewed; preserved\n"
    assert parse_compile_skip_reason(stdout_human, "mara") == \
        "human_override / human_reviewed; preserved"

    # 3. EDA-incomplete skip (missing DARs path at line 663).
    stdout_eda = "  skip ekpo — EDA incomplete; missing: ['dimensions', 'magnitude']\n"
    reason = parse_compile_skip_reason(stdout_eda, "ekpo")
    assert reason is not None
    assert reason.startswith("EDA incomplete")
    assert "dimensions" in reason

    # 4. Write path — compile actually produced a row; no skip line
    # for the target table in stdout.
    stdout_write = (
        "  compiled equi — alias='eq', entity_class='equipment', ...\n"
        "  Wrote 3 rows to domain_analysis_results.csv\n"
        "  dbt seed OK\n"
    )
    assert parse_compile_skip_reason(stdout_write, "equi") is None

    # 5. Empty / missing stdout — helper doesn't crash, returns None.
    assert parse_compile_skip_reason("", "equi") is None
    assert parse_compile_skip_reason(None, "equi") is None  # type: ignore

    # 6. Skip line for a DIFFERENT table must not match this table's
    # request (defense against misleading the toast with a neighbor's
    # skip reason).
    stdout_other = "  skip mkpf — ontology coverage exists in dbt_column_lineage\n"
    assert parse_compile_skip_reason(stdout_other, "equi") is None
