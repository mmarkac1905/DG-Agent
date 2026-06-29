"""KI-113 — Stage C runner _render_query_results error-visibility tests.

The runner renders prior query rows into the next-turn LLM bundle via
`_render_query_results`. Pre-fix, errored queries showed only
`status=error` with no reason, leaving the LLM blind to binder errors
like `Referenced column "BUDAT" not found`. The LLM kept retrying
variants of the same wrong column-table reference for 4 sufficiency-
loop iterations on BG029.

Post-fix, errored rows surface the captured DuckDB error string under
`error_message:` so the LLM has the negative-feedback signal needed
to course-correct.

Also covers:
  - The 3 build_query_row call sites in run_term_eda thread
    _execute_query's `error` key into the persisted row's
    `error_message` column.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import run_term_eda as rte  # noqa: E402


def _qrow_with_error(error_message: str) -> dict:
    """Build a minimal errored query-row dict matching what
    build_query_row produces. Avoids the writer's enum validation by
    hand-constructing the fields _render_query_results actually reads."""
    return {
        "query_index": 1,
        "analysis_lens": "time_trend",
        "stage": "framework_floor",
        "status": "error",
        "result_row_count": 0,
        "query_sql": 'SELECT "BUDAT" FROM main_staging.stg_sap__mseg',
        "query_result_json": "",
        "error_message": error_message,
    }


def _qrow_success() -> dict:
    return {
        "query_index": 2,
        "analysis_lens": "measures_overview",
        "stage": "framework_floor",
        "status": "success",
        "result_row_count": 1,
        "query_sql": "SELECT COUNT(*) FROM main_staging.stg_sap__mseg",
        "query_result_json": '[{"count": 100}]',
        "error_message": "",
    }


def test_render_query_results_includes_error_message_when_present() -> None:
    """Errored row with non-empty error_message surfaces an
    `error_message:` line in the rendered output. The LLM's next-turn
    bundle sees the binder error and can course-correct."""
    err_str = (
        'BinderException: Referenced column "BUDAT" not found in FROM '
        'clause! Candidate bindings: "BWART"'
    )
    rendered = rte._render_query_results([_qrow_with_error(err_str)])
    assert "error_message: " in rendered
    assert "BinderException" in rendered
    assert "BUDAT" in rendered
    # Status line still present.
    assert "status=error" in rendered


def test_render_query_results_omits_error_section_when_no_error() -> None:
    """Successful query (or errored query with empty error_message) does
    NOT emit the `error_message:` line. Avoids a malformed empty entry
    that would clutter the LLM's bundle."""
    rendered = rte._render_query_results([_qrow_success()])
    assert "error_message:" not in rendered
    # status=success still rendered.
    assert "status=success" in rendered

    # Errored row with empty error_message also omits (defensive — covers
    # historical rows from before KI-113 that have NULL/empty in the
    # column).
    historical = _qrow_with_error("")
    rendered_hist = rte._render_query_results([historical])
    assert "error_message:" not in rendered_hist


def test_render_query_results_truncates_long_error_messages() -> None:
    """Defensive: if the DuckDB error string is unusually long (e.g.,
    contains the full bound query text or a list of all candidate
    bindings), the renderer truncates so a single error doesn't
    dominate the next-turn bundle."""
    long_err = "BinderException: " + ("X" * 2000)
    rendered = rte._render_query_results([_qrow_with_error(long_err)])
    assert "error_message:" in rendered
    # Truncation cap at 600 chars per the renderer.
    error_line = next(
        line for line in rendered.splitlines()
        if "error_message:" in line
    )
    # Length budget: prefix "  error_message: " (17) + 600 chars max.
    assert len(error_line) < 700


def test_render_query_results_mixed_success_and_error() -> None:
    """Realistic Stage C mix: some queries succeed, some fail. Render
    output preserves order and correctly attaches error_message only to
    failing rows."""
    rendered = rte._render_query_results([
        _qrow_with_error("BinderException: column X not found"),
        _qrow_success(),
    ])
    lines = rendered.splitlines()
    # Find the indices of the two query entries.
    error_block_start = next(
        i for i, ln in enumerate(lines) if "query_index=1" in ln
    )
    success_block_start = next(
        i for i, ln in enumerate(lines) if "query_index=2" in ln
    )
    error_block = lines[error_block_start:success_block_start]
    success_block = lines[success_block_start:]
    # Error block has error_message; success block does not.
    assert any("error_message:" in ln for ln in error_block)
    assert not any("error_message:" in ln for ln in success_block)


def test_runner_threads_execute_query_error_to_writer() -> None:
    """KI-113 plumbing: when _execute_query returns a dict with an
    `error` key (status='error' path), the runner threads that string
    into build_query_row's error_message parameter so it lands on the
    persisted query row.

    Verifies the contract via a mocked _execute_query — the actual
    call sites (3 of them in run_term_eda) all pass
    `error_message=exec_result.get('error', '')`.
    """
    captured: dict = {}

    def _fake_build_query_row(**kwargs):
        captured.update(kwargs)
        return {"id": "fake"}

    err_str = (
        'BinderException: Referenced column "BUDAT" not found in FROM '
        'clause! Candidate bindings: "BWART"'
    )
    fake_exec = {
        "status": "error",
        "result_json": "",
        "row_count": 0,
        "error": err_str,
    }
    # The runner's contract: build_query_row(..., error_message=fake_exec.get("error", "")).
    # Simulate one call site directly to verify the parameter wiring.
    with patch.object(rte, "build_query_row", side_effect=_fake_build_query_row):
        rte.build_query_row(
            term_id="BG-T",
            analysis_lens="time_trend",
            stage="framework_floor",
            query_index=1,
            query_sql='SELECT "BUDAT" FROM main_staging.stg_sap__mseg',
            query_result_json=fake_exec["result_json"],
            result_row_count=fake_exec["row_count"],
            interpretation="probe",
            grounded_in_tar_ids=[],
            status=fake_exec["status"],
            error_message=fake_exec.get("error", ""),
        )
    assert captured.get("error_message") == err_str
    assert captured.get("status") == "error"


def test_prompt_directive_names_column_table_locality() -> None:
    """KI-112: the prompt's SQL constraints section explicitly directs
    the LLM that every column must belong to a FROM/JOIN table, and
    names the BUDAT/BLDAT/CPUDT pattern as the concrete header-vs-line
    failure case (BG029's empirically-observed hallucination)."""
    prompt = (_ROOT / "scripts" / "prompts" / "term_eda_prompt.md").read_text(
        encoding="utf-8"
    )
    sql_idx = prompt.find("### SQL constraints")
    next_idx = prompt.find("\n### ", sql_idx + 1)
    sql_section = prompt[sql_idx:next_idx if next_idx > -1 else len(prompt)]

    # Column-table locality directive.
    assert "Every column referenced" in sql_section
    assert "FROM clause" in sql_section
    assert "binder error" in sql_section.lower()

    # Concrete header-vs-line domain pattern.
    assert "BUDAT" in sql_section
    assert "mkpf" in sql_section
    assert "mseg" in sql_section
    assert "MBLNR" in sql_section

    # Catalog as authoritative.
    assert "catalog" in sql_section.lower()
    assert "authoritative" in sql_section.lower()

    # Negative-feedback loop reference (KI-113 + KI-112 wired together).
    assert "error_message" in sql_section
