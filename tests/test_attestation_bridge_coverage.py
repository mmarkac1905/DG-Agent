"""Option B Phase 3 — tests for the conditional bridge_coverage
attestation check (OQ-3a Option β).

Coverage:
  - DARs exist + field populated -> passes.
  - DARs exist + field empty -> fails.
  - DARs exist + field missing -> fails.
  - DARs exist + field non-list -> fails.
  - No DARs in scope + field empty -> passes (always-emit honored).
  - No DARs in scope + field missing entirely -> still passes
    (helper's job is conditional check only; attestation_complete()
    handles the always-emit half).
  - ATTESTATION_FIELDS includes the new field (always-emit half).
  - attestation_complete() requires the new field.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import _bridge_coverage_gate as g  # noqa: E402
import run_term_injection as rti  # noqa: E402


def _dar_stub(dar_id: str = "DAR-00527") -> dict:
    return {
        "_dar_id": dar_id,
        "bridge": {"from_table": "seri", "to_table": "mseg",
                   "via_keys_from_to_mid": ["MBLNR"],
                   "from_to_mid_to_columns": ["MBLNR"]},
        "filter_column": {"table": "mseg", "column": "BWART"},
        "unreachable_values": ["201"],
    }


# ----- helper-level tests --------------------------------------------

def test_check_attestation_dars_exist_field_populated_passes():
    propose = {"bridge_coverage_consulted": ["DAR-00527"]}
    ok, msg = g._check_bridge_coverage_attestation(
        propose, conn=None, scope_tables=["seri", "mseg"],
        dars_override=[_dar_stub()],
    )
    assert ok is True
    assert msg is None


def test_check_attestation_dars_exist_field_empty_fails():
    propose = {"bridge_coverage_consulted": []}
    ok, msg = g._check_bridge_coverage_attestation(
        propose, conn=None, scope_tables=["seri", "mseg"],
        dars_override=[_dar_stub()],
    )
    assert ok is False
    assert msg is not None
    assert "DAR-00527" in msg or "empty" in msg.lower()


def test_check_attestation_dars_exist_field_missing_fails():
    propose = {}  # field missing entirely
    ok, msg = g._check_bridge_coverage_attestation(
        propose, conn=None, scope_tables=["seri", "mseg"],
        dars_override=[_dar_stub()],
    )
    assert ok is False
    assert msg is not None
    assert "missing" in msg.lower() or "list" in msg.lower()


def test_check_attestation_dars_exist_field_non_list_fails():
    propose = {"bridge_coverage_consulted": "DAR-00527"}  # string, not list
    ok, msg = g._check_bridge_coverage_attestation(
        propose, conn=None, scope_tables=["seri", "mseg"],
        dars_override=[_dar_stub()],
    )
    assert ok is False
    assert msg is not None


def test_check_attestation_no_dars_field_empty_passes():
    propose = {"bridge_coverage_consulted": []}
    ok, msg = g._check_bridge_coverage_attestation(
        propose, conn=None, scope_tables=["seri", "mseg"],
        dars_override=[],
    )
    assert ok is True
    assert msg is None


def test_check_attestation_no_dars_field_missing_passes():
    """Helper's only job is conditional check. attestation_complete()
    enforces always-emit; this helper just gates on DARs-vs-field."""
    propose = {}
    ok, msg = g._check_bridge_coverage_attestation(
        propose, conn=None, scope_tables=["seri", "mseg"],
        dars_override=[],
    )
    assert ok is True
    assert msg is None


def test_check_attestation_propose_not_dict_fails_when_dars_present():
    ok, msg = g._check_bridge_coverage_attestation(
        propose=None, conn=None, scope_tables=["seri", "mseg"],
        dars_override=[_dar_stub()],
    )
    assert ok is False


# ----- always-emit half (ATTESTATION_FIELDS + attestation_complete) -

def test_attestation_fields_includes_bridge_coverage_consulted():
    assert "bridge_coverage_consulted" in rti.ATTESTATION_FIELDS


def test_attestation_complete_requires_new_field():
    """All 8 fields must be present + lists for attestation_complete
    to return True. Missing bridge_coverage_consulted -> False."""
    base = {f: [] for f in rti.ATTESTATION_FIELDS}
    assert rti.attestation_complete(base) is True
    no_bc = {k: v for k, v in base.items()
             if k != "bridge_coverage_consulted"}
    assert rti.attestation_complete(no_bc) is False
    none_bc = {**base, "bridge_coverage_consulted": None}
    assert rti.attestation_complete(none_bc) is False


# ----- Option B Phase 4 — ATTESTATION_FIELDS split tests --------------

def test_iter_response_attestation_includes_all_attestation_fields():
    """iter_response_attestation in the iteration loop is built from
    a comprehension over ATTESTATION_FIELDS_ITERATION (Gap B refactor).
    Verify shape: every iteration-contract field is a key, value is a
    list (empty or populated). Future attestation field additions
    flow through the constant; no double-edit needed at line ~1560.

    Built here using the same comprehension expression as the runner."""
    propose = {
        "ontology_consumed": ["fact_x"],
        "domain_facts_consumed": [],
        "analysis_findings_consumed": ["DAR-00001"],
        "dar_consumed": ["DAR-00001"],
        "prior_bar_consumed": [],
        "semantic_model_consumed": ["mseg"],
        "dbt_semantic_model_consumed": ["stg_sap__mseg"],
        "bridge_coverage_consulted": ["DAR-00527"],
    }
    iter_response_attestation = {
        field: (propose.get(field, []) or [])
        for field in rti.ATTESTATION_FIELDS_ITERATION
    }
    # Every ITERATION field is present.
    for field in rti.ATTESTATION_FIELDS_ITERATION:
        assert field in iter_response_attestation, f"{field} missing"
        assert isinstance(iter_response_attestation[field], list)
    # bridge_coverage_consulted (Phase 3) is present.
    assert "bridge_coverage_consulted" in iter_response_attestation
    assert iter_response_attestation["bridge_coverage_consulted"] == ["DAR-00527"]


def test_attestation_complete_iteration_requires_10_fields():
    """attestation_complete(response, ATTESTATION_FIELDS_ITERATION)
    requires all 10 iteration-contract fields (8 pre-C3 + tars_consulted
    + stage_a_blockers_consumed). Missing bridge_coverage_consulted ->
    False (regresses to a 9-field pre-C4 shape)."""
    assert len(rti.ATTESTATION_FIELDS_ITERATION) == 10
    base = {f: [] for f in rti.ATTESTATION_FIELDS_ITERATION}
    assert rti.attestation_complete(base, rti.ATTESTATION_FIELDS_ITERATION) is True
    # Drop bridge_coverage_consulted -> iteration gate fails.
    no_bc = {k: v for k, v in base.items()
             if k != "bridge_coverage_consulted"}
    assert rti.attestation_complete(no_bc, rti.ATTESTATION_FIELDS_ITERATION) is False


def test_attestation_complete_finalization_requires_7_fields():
    """attestation_complete(response, ATTESTATION_FIELDS_FINALIZATION)
    requires the 7 original fields. Missing any one -> False."""
    assert len(rti.ATTESTATION_FIELDS_FINALIZATION) == 7
    assert "bridge_coverage_consulted" not in rti.ATTESTATION_FIELDS_FINALIZATION
    base = {f: [] for f in rti.ATTESTATION_FIELDS_FINALIZATION}
    assert rti.attestation_complete(base, rti.ATTESTATION_FIELDS_FINALIZATION) is True
    # Drop dar_consumed -> finalization gate fails.
    no_dar = {k: v for k, v in base.items() if k != "dar_consumed"}
    assert rti.attestation_complete(no_dar, rti.ATTESTATION_FIELDS_FINALIZATION) is False


def test_attestation_complete_finalization_passes_without_bridge_coverage_consulted():
    """The semantic boundary: finalization summarizes, doesn't consult
    bridge_coverage DARs. A finalization response missing the field
    must still pass the finalization gate (Gap C — closes the Phase 4
    v1 regression where finalization-attestation overwrote a correct
    hard_stop_bridge_unreachable convergence_reason)."""
    finalize_response = {
        "ontology_consumed": [],
        "domain_facts_consumed": [],
        "analysis_findings_consumed": [],
        "dar_consumed": [],
        "prior_bar_consumed": [],
        "semantic_model_consumed": [],
        "dbt_semantic_model_consumed": [],
        # NO bridge_coverage_consulted — by design.
    }
    assert rti.attestation_complete(
        finalize_response, rti.ATTESTATION_FIELDS_FINALIZATION
    ) is True
    # Same response would FAIL the iteration gate (correctly).
    assert rti.attestation_complete(
        finalize_response, rti.ATTESTATION_FIELDS_ITERATION
    ) is False
