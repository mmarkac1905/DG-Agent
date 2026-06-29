"""C4 (Theme 1 sub-item 5) — tests for the stage_a_blockers_consumed
attestation field and Stage A blocker citation discipline.

Coverage (mirrors test_attestation_tars.py, simplified because there
is no conditional gate per OQ-C4-1's lean):
  - ATTESTATION_FIELDS_ITERATION includes stage_a_blockers_consumed.
  - ATTESTATION_FIELDS_FINALIZATION excludes it (Gap C iteration/
    finalization split — finalization summarizes, doesn't independently
    re-consult Stage A blockers).
  - attestation_complete() iteration gate requires the field.
  - attestation_complete() finalization gate passes without it.
  - iter_response_attestation comprehension auto-picks up the new field.
  - BAR-write _union_attestation_from_trace_and_finalize call pulls
    stage_a_blockers_consumed from trace[N].response (iteration-only
    attestation, same shape as bridge_coverage_consulted / tars_consulted).
  - Empty list and iter{N}.b{I} ID-format examples are accepted.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import run_term_injection as rti  # noqa: E402


# ----- ATTESTATION_FIELDS shape -----------------------------------------

def test_attestation_fields_iteration_includes_stage_a_blockers_consumed():
    """C4 Step 3 — ATTESTATION_FIELDS_ITERATION grew from 9 to 10 fields,
    with stage_a_blockers_consumed appended. The 9 pre-C4 fields stay
    in place."""
    assert "stage_a_blockers_consumed" in rti.ATTESTATION_FIELDS_ITERATION
    assert len(rti.ATTESTATION_FIELDS_ITERATION) == 10


def test_attestation_fields_finalization_excludes_stage_a_blockers_consumed():
    """Gap C semantic split: finalization summarizes the iteration trace,
    it does NOT re-consult Stage A blockers from the bundle.
    stage_a_blockers_consumed is iteration-only, mirroring
    bridge_coverage_consulted / tars_consulted exclusion."""
    assert (
        "stage_a_blockers_consumed"
        not in rti.ATTESTATION_FIELDS_FINALIZATION
    )
    assert len(rti.ATTESTATION_FIELDS_FINALIZATION) == 7


# ----- attestation_complete() gate behavior -----------------------------

def test_attestation_complete_iteration_requires_stage_a_blockers_consumed():
    """Drop stage_a_blockers_consumed from a complete iteration response →
    the iteration gate must fail (regresses to the pre-C4 9-field shape).
    This is the always-emit half of C4's contract."""
    base = {f: [] for f in rti.ATTESTATION_FIELDS_ITERATION}
    assert rti.attestation_complete(
        base, rti.ATTESTATION_FIELDS_ITERATION) is True
    no_sab = {k: v for k, v in base.items()
              if k != "stage_a_blockers_consumed"}
    assert rti.attestation_complete(
        no_sab, rti.ATTESTATION_FIELDS_ITERATION) is False


def test_attestation_complete_finalization_passes_without_stage_a_blockers_consumed():
    """Semantic boundary: a finalization response missing
    stage_a_blockers_consumed must still pass the FINALIZATION gate.
    Mirrors the Phase 4 Gap C closure pattern for iteration-only
    attestation fields."""
    finalize_response = {
        "ontology_consumed": [],
        "domain_facts_consumed": [],
        "analysis_findings_consumed": [],
        "dar_consumed": [],
        "prior_bar_consumed": [],
        "semantic_model_consumed": [],
        "dbt_semantic_model_consumed": [],
        # NO stage_a_blockers_consumed — by design.
    }
    assert rti.attestation_complete(
        finalize_response, rti.ATTESTATION_FIELDS_FINALIZATION) is True


# ----- Phase 4 Gap B comprehension --------------------------------------

def test_iter_response_attestation_includes_stage_a_blockers_consumed():
    """The iteration loop's iter_response_attestation dict is built via
    a comprehension over ATTESTATION_FIELDS_ITERATION (Gap B). C4's
    addition flows through that constant — no double-edit at the
    callsite. The blocker IDs the LLM emitted survive the comprehension."""
    propose = {
        "ontology_consumed": [],
        "domain_facts_consumed": [],
        "analysis_findings_consumed": [],
        "dar_consumed": [],
        "prior_bar_consumed": [],
        "semantic_model_consumed": [],
        "dbt_semantic_model_consumed": [],
        "bridge_coverage_consulted": [],
        "tars_consulted": [],
        "stage_a_blockers_consumed": ["iter1.b0", "iter1.b1"],
    }
    iter_response_attestation = {
        field: (propose.get(field, []) or [])
        for field in rti.ATTESTATION_FIELDS_ITERATION
    }
    assert "stage_a_blockers_consumed" in iter_response_attestation
    assert iter_response_attestation["stage_a_blockers_consumed"] == [
        "iter1.b0", "iter1.b1"]


# ----- BAR-write Phase 4 Gap D union -----------------------------------

def test_bar_write_unions_stage_a_blockers_consumed_from_trace_response():
    """The BAR-write payload calls _union_attestation_from_trace_and_finalize
    for stage_a_blockers_consumed. Because the field is iteration-only
    (FINALIZATION excludes it), the helper's value comes from
    trace[N].response (Source 3 — iteration-only support added in
    Phase 4). Verify the union de-dups and preserves order across
    iterations."""
    iteration_trace = [
        {
            "gates_result": {},
            "response": {
                "stage_a_blockers_consumed": ["iter1.b0", "iter1.b1"]},
        },
        {
            "gates_result": {},
            "response": {
                "stage_a_blockers_consumed": ["iter1.b1", "iter1.b2"]},
        },
    ]
    finalize = {}  # finalization doesn't echo this field (Gap C).
    result = rti._union_attestation_from_trace_and_finalize(
        iteration_trace, finalize, "stage_a_blockers_consumed")
    # Order-preserving dedup; iter-0 citations survive.
    assert result == ["iter1.b0", "iter1.b1", "iter1.b2"]


def test_stage_a_blockers_consumed_empty_list_acceptable():
    """Empty trace + empty finalize → []. Mirrors the always-emit
    semantics: BAR persists `[]` (not NULL) when no blockers consulted."""
    result = rti._union_attestation_from_trace_and_finalize(
        [], {}, "stage_a_blockers_consumed")
    assert result == []


def test_stage_a_blockers_consumed_with_id_format_validates():
    """ID format `iter{N}.b{I}` is the verbatim contract the LLM emits.
    The runner's union helper passes IDs through unchanged (no parsing,
    no validation) — the discipline is enforced by the prompt and the
    auditability test below, not by the runtime helper."""
    # The helper preserves IDs verbatim regardless of format.
    iteration_trace = [{
        "gates_result": {},
        "response": {"stage_a_blockers_consumed": [
            "iter1.b0", "iter2.b3", "iter1.b10",
        ]},
    }]
    result = rti._union_attestation_from_trace_and_finalize(
        iteration_trace, {}, "stage_a_blockers_consumed")
    assert result == ["iter1.b0", "iter2.b3", "iter1.b10"]
    # Verify the iteration prompt teaches the format so the LLM emits it
    # correctly. (Format-validation contract: prompt + auditability;
    # not runtime enforcement, per OQ-C4-1's no-conditional-gate lean.)
    prompt_path = (
        _ROOT / "scripts" / "prompts" / "term_injection_iteration_prompt.md"
    )
    text = prompt_path.read_text(encoding="utf-8")
    assert "iter{N}.b{I}" in text
    assert "iter1.b0" in text
