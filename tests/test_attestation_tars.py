"""C3 (Theme 1 sub-items 1+2) — tests for the tars_consulted attestation
field and TAR-NNNNN citation discipline.

Coverage (mirrors the bridge_coverage_consulted test scope, simplified
because there is no conditional gate per OQ-C3-2):
  - ATTESTATION_FIELDS_ITERATION includes tars_consulted (always-emit).
  - ATTESTATION_FIELDS_FINALIZATION excludes tars_consulted (Gap C
    iteration/finalization split: finalization summarizes the trace,
    doesn't independently re-consult TARs).
  - attestation_complete() iteration gate requires tars_consulted.
  - attestation_complete() finalization gate passes without it.
  - iter_response_attestation comprehension (Phase 4 Gap B) auto-picks
    up the new field generically.
  - BAR-write _union_attestation_from_trace_and_finalize call pulls
    tars_consulted from trace[N].response (iteration-only attestation,
    same shape as bridge_coverage_consulted).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import run_term_injection as rti  # noqa: E402


# ----- ATTESTATION_FIELDS shape -----------------------------------------

def test_attestation_fields_iteration_includes_tars_consulted():
    """C3 Step 1 — ATTESTATION_FIELDS_ITERATION grew from 8 to 9 fields,
    with tars_consulted appended. C4 subsequently grew it to 10
    (stage_a_blockers_consumed); this test verifies tars_consulted's
    membership without locking the count to 9."""
    assert "tars_consulted" in rti.ATTESTATION_FIELDS_ITERATION
    assert len(rti.ATTESTATION_FIELDS_ITERATION) == 10


def test_attestation_fields_finalization_excludes_tars_consulted():
    """Gap C semantic split: finalization summarizes the iteration trace
    (it re-states the converged SQL and records audit context), it does
    NOT re-consult TARs from the bundle. tars_consulted is iteration-
    only, mirroring bridge_coverage_consulted's exclusion."""
    assert "tars_consulted" not in rti.ATTESTATION_FIELDS_FINALIZATION
    assert len(rti.ATTESTATION_FIELDS_FINALIZATION) == 7


# ----- attestation_complete() gate behavior -----------------------------

def test_attestation_complete_iteration_requires_tars_consulted():
    """Drop tars_consulted from a complete iteration response → the
    iteration gate must fail (regresses to the pre-C3 8-field shape).
    This is the always-emit half of C3's contract."""
    base = {f: [] for f in rti.ATTESTATION_FIELDS_ITERATION}
    assert rti.attestation_complete(
        base, rti.ATTESTATION_FIELDS_ITERATION) is True
    no_tars = {k: v for k, v in base.items() if k != "tars_consulted"}
    assert rti.attestation_complete(
        no_tars, rti.ATTESTATION_FIELDS_ITERATION) is False


def test_attestation_complete_finalization_passes_without_tars_consulted():
    """The semantic boundary: a finalization response missing
    tars_consulted must still pass the FINALIZATION gate. Mirrors the
    Phase 4 Gap C closure pattern for bridge_coverage_consulted —
    iteration-only attestation fields don't bleed into finalization."""
    finalize_response = {
        "ontology_consumed": [],
        "domain_facts_consumed": [],
        "analysis_findings_consumed": [],
        "dar_consumed": [],
        "prior_bar_consumed": [],
        "semantic_model_consumed": [],
        "dbt_semantic_model_consumed": [],
        # NO tars_consulted — by design.
    }
    assert rti.attestation_complete(
        finalize_response, rti.ATTESTATION_FIELDS_FINALIZATION) is True


# ----- Phase 4 Gap B comprehension --------------------------------------

def test_iter_response_attestation_includes_tars_consulted():
    """The iteration loop's iter_response_attestation dict is built via
    a comprehension over ATTESTATION_FIELDS_ITERATION (Gap B). C3's
    addition flows through that constant — no double-edit at the
    callsite. The TAR ids the LLM emitted survive the comprehension."""
    propose = {
        "ontology_consumed": ["fact_x"],
        "domain_facts_consumed": [],
        "analysis_findings_consumed": [],
        "dar_consumed": [],
        "prior_bar_consumed": [],
        "semantic_model_consumed": [],
        "dbt_semantic_model_consumed": [],
        "bridge_coverage_consulted": [],
        "tars_consulted": ["TAR-00012", "TAR-00045"],
    }
    iter_response_attestation = {
        field: (propose.get(field, []) or [])
        for field in rti.ATTESTATION_FIELDS_ITERATION
    }
    assert "tars_consulted" in iter_response_attestation
    assert iter_response_attestation["tars_consulted"] == [
        "TAR-00012", "TAR-00045"]


# ----- BAR-write Phase 4 Gap D union -----------------------------------

def test_bar_write_unions_tars_consulted_from_trace_response():
    """The BAR-write payload calls
    _union_attestation_from_trace_and_finalize for tars_consulted.
    Because tars_consulted is iteration-only (FINALIZATION excludes
    it), the helper's value comes from trace[N].response (Source 3,
    added in Phase 4 to support iteration-only fields). Verify the
    union de-dups and preserves order across iterations."""
    iteration_trace = [
        {
            "gates_result": {},  # Layer A/B threading not used for TARs
            "response": {"tars_consulted": ["TAR-00001", "TAR-00002"]},
        },
        {
            "gates_result": {},
            "response": {"tars_consulted": ["TAR-00002", "TAR-00007"]},
        },
    ]
    # Finalization doesn't echo tars_consulted (Gap C); pass empty.
    finalize = {}
    result = rti._union_attestation_from_trace_and_finalize(
        iteration_trace, finalize, "tars_consulted")
    # Order-preserving dedup; iter-0 citations survive.
    assert result == ["TAR-00001", "TAR-00002", "TAR-00007"]


def test_bar_write_unions_tars_consulted_handles_empty_trace():
    """Empty trace + empty finalize → empty list. Mirrors the always-
    emit `[]` semantics: the BAR persists `[]` (not NULL) when no TARs
    were consulted across any iteration."""
    result = rti._union_attestation_from_trace_and_finalize(
        [], {}, "tars_consulted")
    assert result == []


# ----- Iteration prompt directives (optional extension) -----------------

def test_iteration_prompt_directives_mention_tars_consulted():
    """C3 Step 4(d) — TERM EDA ANALYTICAL CHARACTERIZATION section
    now teaches TAR citation discipline: the LLM must emit consulted
    TAR-NNNNN ids regardless of row_type, both current-term and
    cross-term prior TARs going into the same field."""
    prompt_path = (
        _ROOT / "scripts" / "prompts" / "term_injection_iteration_prompt.md"
    )
    text = prompt_path.read_text(encoding="utf-8")
    # Field appears in REQUIRED BUNDLE SOURCES list (preamble).
    assert "tars_consulted" in text
    # Citation discipline section is present.
    assert "TAR CITATION DISCIPLINE" in text
    # Both row_types covered.
    assert "row_type='query'" in text
    assert "row_type='sufficiency'" in text
    # Cross-term scope spelled out (line break between "prior" and
    # "TARs" tolerated; the words just need to coexist in the section).
    assert "cross-term prior" in text
    assert "current-term TARs" in text
    # JSON example shows the field.
    assert '"tars_consulted"' in text
