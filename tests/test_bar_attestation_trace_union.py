"""Unit tests for attestation trace/BAR consistency.

Verifies that BAR-level semantic_model_consumed /
dbt_semantic_model_consumed are always a superset of each
trace[N].gates_result.<field>. Run standalone:
  python tests/test_bar_attestation_trace_union.py

Exit 0 on all-pass, 1 on any failure. No pytest dependency.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from run_term_injection import (  # noqa: E402
    _union_attestation_from_trace_and_finalize,
)


def _trace(*iter_sm_lists: list[str]) -> list[dict]:
    """Build a minimal iteration_trace with gates_result.semantic_model_consumed
    populated from each arg."""
    return [
        {"gates_result": {"semantic_model_consumed": list(v)}}
        for v in iter_sm_lists
    ]


def run() -> int:
    failed = 0
    checks: list[tuple[str, bool]] = []

    # Case 1 — single iter, finalize matches trace: BAR == trace[0].
    trace = _trace(["semantic_model:ekbe"])
    finalize = {"semantic_model_consumed": ["semantic_model:ekbe"]}
    bar = _union_attestation_from_trace_and_finalize(
        trace, finalize, "semantic_model_consumed")
    checks.append(("single-iter match", bar == ["semantic_model:ekbe"]))

    # Case 2 — single iter, finalize drops the citation: BAR keeps it from trace.
    trace = _trace(["semantic_model:ekbe"])
    finalize = {"semantic_model_consumed": []}
    bar = _union_attestation_from_trace_and_finalize(
        trace, finalize, "semantic_model_consumed")
    checks.append(("finalize drops, trace rescues", bar == ["semantic_model:ekbe"]))

    # Case 3 — multi iter, BAR is union.
    trace = _trace(["semantic_model:ekbe"], ["semantic_model:ekko"])
    finalize = {"semantic_model_consumed": []}
    bar = _union_attestation_from_trace_and_finalize(
        trace, finalize, "semantic_model_consumed")
    checks.append(("multi-iter union",
                   set(bar) == {"semantic_model:ekbe", "semantic_model:ekko"}))

    # Case 4 — finalize adds a citation not in any trace: also included.
    trace = _trace(["semantic_model:ekbe"])
    finalize = {"semantic_model_consumed": ["semantic_model:ekko"]}
    bar = _union_attestation_from_trace_and_finalize(
        trace, finalize, "semantic_model_consumed")
    checks.append(("finalize-only citation included",
                   set(bar) == {"semantic_model:ekbe", "semantic_model:ekko"}))

    # Case 5 — empty trace, empty finalize: BAR empty.
    bar = _union_attestation_from_trace_and_finalize(
        [], {}, "semantic_model_consumed")
    checks.append(("empty input empty output", bar == []))

    # Case 6 — superset invariant: BAR ⊇ trace[0] for Layer B too.
    trace = [{"gates_result": {
        "dbt_semantic_model_consumed": ["dbt:fact_purchase_orders"]}}]
    finalize = {"dbt_semantic_model_consumed": []}
    bar = _union_attestation_from_trace_and_finalize(
        trace, finalize, "dbt_semantic_model_consumed")
    trace0 = trace[0]["gates_result"]["dbt_semantic_model_consumed"]
    checks.append(("layer B superset invariant", set(bar) >= set(trace0)))

    # Case 7 — order-preserving dedup.
    trace = _trace(["a", "b"], ["b", "c"])
    finalize = {"semantic_model_consumed": ["a"]}
    bar = _union_attestation_from_trace_and_finalize(
        trace, finalize, "semantic_model_consumed")
    checks.append(("order-preserving dedup", bar == ["a", "b", "c"]))

    # Case 8 — Option B Phase 4 Gap D: iteration-only attestation field
    # (bridge_coverage_consulted) lives in trace[N].response, not in
    # trace[N].gates_result. Helper must read source 3 (response).
    # Finalize.<field> is empty by Gap C semantic split. Without the
    # third source, BAR.bridge_coverage_consulted would silently drop
    # the iteration LLM's citation.
    trace_phase4 = [{
        "gates_result": {},  # iteration-only field NOT threaded here
        "response": {"bridge_coverage_consulted": ["DAR-00527"]},
    }]
    finalize_phase4 = {}  # post-Gap-C — finalize doesn't attest this
    bar = _union_attestation_from_trace_and_finalize(
        trace_phase4, finalize_phase4, "bridge_coverage_consulted")
    checks.append(("Phase 4 Gap D: response-source rescue",
                   bar == ["DAR-00527"]))

    # Case 9 — Phase 4 multi-iter response union with order preservation.
    trace_multi = [
        {"gates_result": {},
         "response": {"bridge_coverage_consulted": ["DAR-00527"]}},
        {"gates_result": {},
         "response": {"bridge_coverage_consulted": ["DAR-00528", "DAR-00527"]}},
    ]
    bar = _union_attestation_from_trace_and_finalize(
        trace_multi, {}, "bridge_coverage_consulted")
    checks.append(("Phase 4 Gap D: multi-iter response union",
                   bar == ["DAR-00527", "DAR-00528"]))

    for name, ok in checks:
        if ok:
            print(f"  [pass] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name}")
    print(f"\n{len(checks) - failed}/{len(checks)} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
