"""C5 closure 1/4 — tests for [REACHABILITY VIOLATIONS] prompt block.

Coverage:
  - Block is empty string for the existing scope_sanity-no trigger
    path (so the prompt template renders unchanged).
  - Block is populated for hard_stop_bridge_unreachable; reads
    `gates_result.bridge_violations` (list[str]) from the last
    iteration and renders bullets.
  - Block handles hard_stop_bridge_attestation_missing's asymmetric
    `gates_result.violation` (singular str) by wrapping into a
    single-element bullet list.
  - Block format matches the brief: header + lead-in + bullets +
    instructional trailer; preserves gate-output text verbatim.
  - Defensive: empty trace, missing gates_result, missing field
    all degrade to empty string.
  - Prompt template substitution: rendered template contains the
    block when populated and contains no orphaned placeholder when
    empty.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from run_term_injection import (  # noqa: E402
    _format_reachability_violations_block,
    _fill_template,
    _load_prompt,
)


def _trace_with_violations(violations: list[str]) -> list[dict]:
    """Last entry carries the bridge_unreachable gate output."""
    return [{
        "iteration_num": 1,
        "gates_result": {
            "compile": "not_evaluated",
            "run": "not_evaluated",
            "bridge_coverage": "fail",
            "bridge_violations": violations,
        },
    }]


def _trace_with_attestation_violation(message: str) -> list[dict]:
    """Last entry carries the bridge_attestation_missing gate output
    (singular `violation` str, not a list)."""
    return [{
        "iteration_num": 1,
        "gates_result": {
            "compile": "not_evaluated",
            "run": "not_evaluated",
            "bridge_coverage_attestation": "fail",
            "violation": message,
        },
    }]


# ----- _format_reachability_violations_block -------------------------

def test_block_empty_for_scope_sanity_path():
    """Existing trigger (consecutive scope_sanity=no) → no block.
    The convergence_reason for that path is hard_stop_scope_mismatch
    — not in the Option B set."""
    trace = [{"iteration_num": 1, "gates_result": {}, "scope_sanity_answer": "no"}]
    block = _format_reachability_violations_block(trace, "hard_stop_scope_mismatch")
    assert block == ""


def test_block_empty_for_other_convergence_reasons():
    """Defensive: hard_stop_max_iters / converged_* / None all → empty."""
    trace = [{"iteration_num": 1, "gates_result": {}}]
    for cr in ("hard_stop_max_iters", "converged_low_alignment",
               "hard_stop_oscillation", "hard_stop_attestation_failure",
               None, ""):
        assert _format_reachability_violations_block(trace, cr) == "", \
            f"expected empty for convergence_reason={cr!r}"


def test_block_populated_for_bridge_unreachable():
    """Path (b) hard_stop_bridge_unreachable populates from
    gates_result.bridge_violations (list[str]); each violation
    becomes a bullet line."""
    violations = [
        "Bridge seri.MBLNR -> mseg.MBLNR; filter mseg.BWART='201' "
        "empirically unreachable per DAR-00527 (reachable: ['101'])",
        "Bridge seri.MBLNR -> mseg.MBLNR; filter mseg.BWART IN ('122', '161') "
        "all values unreachable per DAR-00527",
    ]
    block = _format_reachability_violations_block(
        _trace_with_violations(violations),
        "hard_stop_bridge_unreachable",
    )
    assert "[REACHABILITY VIOLATIONS]" in block
    assert "Option B's runtime gate" in block
    # Both violations appear verbatim (gate output preserved).
    assert violations[0] in block
    assert violations[1] in block
    # Bullet rendering.
    assert f"- {violations[0]}" in block
    assert f"- {violations[1]}" in block
    # Trailer instructs the LLM how to use the violations.
    assert "focus your sourcing recommendations" in block
    assert "specific reachability gaps" in block


def test_block_populated_for_attestation_missing_singular():
    """Path (b) hard_stop_bridge_attestation_missing reads the
    singular `violation` string and renders a single bullet."""
    msg = ("bridge_coverage_consulted attestation expected (DARs exist "
           "for scope) but field is missing or not a list.")
    block = _format_reachability_violations_block(
        _trace_with_attestation_violation(msg),
        "hard_stop_bridge_attestation_missing",
    )
    assert "[REACHABILITY VIOLATIONS]" in block
    assert f"- {msg}" in block


def test_block_empty_when_gate_output_is_empty():
    """Defensive: convergence_reason is Option B but gates_result has
    no violations payload (e.g., upstream bug). Helper degrades to
    empty rather than rendering an empty bullet list."""
    trace_empty_list = _trace_with_violations([])
    assert _format_reachability_violations_block(
        trace_empty_list, "hard_stop_bridge_unreachable"
    ) == ""
    trace_no_field = [{"iteration_num": 1, "gates_result": {}}]
    assert _format_reachability_violations_block(
        trace_no_field, "hard_stop_bridge_unreachable"
    ) == ""
    trace_empty_str = _trace_with_attestation_violation("")
    assert _format_reachability_violations_block(
        trace_empty_str, "hard_stop_bridge_attestation_missing"
    ) == ""


def test_block_empty_when_trace_is_empty():
    """No iterations → no block (defensive)."""
    assert _format_reachability_violations_block(
        [], "hard_stop_bridge_unreachable"
    ) == ""


# ----- prompt template substitution ----------------------------------

def test_prompt_template_substitutes_block_when_populated():
    """Render the C5 prompt with a populated reachability block —
    the rendered template contains the [REACHABILITY VIOLATIONS]
    section between [TERM CONTEXT] and [CATALOG]."""
    tmpl = _load_prompt("c5_sourcing_recommendation_prompt.md")
    block = _format_reachability_violations_block(
        _trace_with_violations(["sample violation A", "sample violation B"]),
        "hard_stop_bridge_unreachable",
    )
    rendered = _fill_template(tmpl, {
        "term_name": "X", "term_definition": "X",
        "term_grain": "X", "term_conditions": "X",
        "confirmed_scope_tables": "X",
        "last_iteration_sql": "X", "last_iteration_reflection": "X",
        "scope_sanity_rationale": "X",
        "reachability_violations": block,
        "catalog_block": "X",
    })
    # Block is present.
    assert "[REACHABILITY VIOLATIONS]" in rendered
    assert "- sample violation A" in rendered
    assert "- sample violation B" in rendered
    # Block sits between [TERM CONTEXT] header and [CATALOG] body header.
    # NB: anchor on unique body-header substring for [CATALOG] — the bare
    # "[CATALOG]" appears in the prompt's preamble too.
    idx_term = rendered.find("[TERM CONTEXT]")
    idx_block = rendered.find("[REACHABILITY VIOLATIONS]")
    idx_catalog_body = rendered.find("[CATALOG] — ground truth")
    assert idx_term < idx_block < idx_catalog_body
    # Placeholder fully substituted (no leakage).
    assert "{reachability_violations}" not in rendered


def test_prompt_template_substitutes_empty_block_cleanly():
    """When triggered via scope_sanity-no path, the block is empty —
    the rendered template has no [REACHABILITY VIOLATIONS] section
    and no orphaned placeholder."""
    tmpl = _load_prompt("c5_sourcing_recommendation_prompt.md")
    rendered = _fill_template(tmpl, {
        "term_name": "X", "term_definition": "X",
        "term_grain": "X", "term_conditions": "X",
        "confirmed_scope_tables": "X",
        "last_iteration_sql": "X", "last_iteration_reflection": "X",
        "scope_sanity_rationale": "X",
        "reachability_violations": "",  # scope_sanity path
        "catalog_block": "X",
    })
    assert "[REACHABILITY VIOLATIONS]" not in rendered
    assert "{reachability_violations}" not in rendered
    # [CATALOG] still follows [TERM CONTEXT] in the template.
    assert "[TERM CONTEXT]" in rendered
    assert "[CATALOG]" in rendered
