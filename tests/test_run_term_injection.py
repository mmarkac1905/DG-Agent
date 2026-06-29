"""Phase 2b unit tests for scripts/run_term_injection.py helpers.

Currently covers:
  - _should_fire_c5: pure trigger detection for the C5 sourcing-
    recommendations layer (tasks/c5_design.md Component 3). Fires when
    the last two iteration trace entries both have
    scope_sanity_answer == "no". Same condition that yields
    convergence_reason == "hard_stop_scope_mismatch" but kept decoupled.

Future Phase 2b/Phase 3 tests for runner integration paths (mock LLM)
should also live in this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from run_term_injection import _should_fire_c5  # noqa: E402


def _entry(answer: str | None) -> dict:
    """Minimal iteration trace entry. Real entries carry many fields;
    only scope_sanity_answer matters for this helper."""
    if answer is None:
        return {"iteration_num": 1}
    return {"iteration_num": 1, "scope_sanity_answer": answer}


def test_c5_trigger_fires_on_consecutive_no() -> None:
    trace = [_entry("yes"), _entry("no"), _entry("no")]
    assert _should_fire_c5(trace) is True


def test_c5_trigger_skips_single_no() -> None:
    """Only one iteration with scope_sanity=no → no trigger."""
    trace = [_entry("yes"), _entry("no")]
    assert _should_fire_c5(trace) is False


def test_c5_trigger_skips_alternating() -> None:
    """no/yes/no — last two are not both 'no'."""
    trace = [_entry("no"), _entry("yes"), _entry("no")]
    assert _should_fire_c5(trace) is False


def test_c5_trigger_skips_no_iterations() -> None:
    assert _should_fire_c5([]) is False


def test_c5_trigger_skips_single_iteration() -> None:
    """Even if it's a 'no', a single iteration cannot satisfy 'consecutive'."""
    assert _should_fire_c5([_entry("no")]) is False


def test_c5_trigger_fires_on_three_consecutive_no() -> None:
    """Triple 'no' — the LAST two are still 'no', trigger fires."""
    trace = [_entry("no"), _entry("no"), _entry("no")]
    assert _should_fire_c5(trace) is True


def test_c5_trigger_uncertain_is_not_no() -> None:
    """'uncertain' (the default fallback per §4a step 9a) does NOT count
    as 'no'. Strict equality only."""
    trace = [_entry("uncertain"), _entry("uncertain")]
    assert _should_fire_c5(trace) is False
    trace = [_entry("no"), _entry("uncertain")]
    assert _should_fire_c5(trace) is False
    trace = [_entry("uncertain"), _entry("no")]
    assert _should_fire_c5(trace) is False


def test_c5_trigger_missing_field_defaults_to_not_no() -> None:
    """Defensive: trace entry without scope_sanity_answer key → not 'no'."""
    trace = [_entry(None), _entry(None)]
    assert _should_fire_c5(trace) is False
    trace = [_entry("no"), _entry(None)]
    assert _should_fire_c5(trace) is False


# ----- C5 closure 1/4 — Option B trigger broadening tests -----------

def test_c5_trigger_bridge_unreachable_single_fire() -> None:
    """Path (b): hard_stop_bridge_unreachable fires C5 even on a
    single iteration. The Option B gate breaks the loop on first fire,
    so consecutive-twice semantics don't apply."""
    trace = [_entry("yes")]  # single iter, scope_sanity=yes (irrelevant for path b)
    assert _should_fire_c5(trace, "hard_stop_bridge_unreachable") is True


def test_c5_trigger_bridge_attestation_missing_single_fire() -> None:
    """Path (b): hard_stop_bridge_attestation_missing also single-fires."""
    trace = [_entry("uncertain")]
    assert _should_fire_c5(trace, "hard_stop_bridge_attestation_missing") is True


def test_c5_trigger_bridge_unreachable_with_empty_trace_still_fires() -> None:
    """Path (b) is convergence-reason-driven; doesn't depend on trace
    length. Defensive — gate could in principle fire pre-trace-append,
    so the trigger must not require an entry."""
    assert _should_fire_c5([], "hard_stop_bridge_unreachable") is True


def test_c5_trigger_other_convergence_reason_does_not_fire() -> None:
    """hard_stop_max_iters / hard_stop_oscillation / converged_*
    are NOT Option B reasons; path (b) doesn't fire. Trace also
    doesn't satisfy path (a) (no consecutive scope_sanity=no), so
    overall result is False."""
    trace = [_entry("yes"), _entry("yes")]
    assert _should_fire_c5(trace, "hard_stop_max_iters") is False
    assert _should_fire_c5(trace, "hard_stop_oscillation") is False
    assert _should_fire_c5(trace, "converged_low_alignment") is False
    assert _should_fire_c5(trace, "hard_stop_attestation_failure") is False


def test_c5_trigger_default_convergence_reason_preserves_path_a() -> None:
    """convergence_reason defaults to None — existing callers that
    don't pass the new param see path (a) only. Verifies backward
    compatibility of the signature change."""
    # Path (a) satisfied → True regardless of None.
    trace_a = [_entry("no"), _entry("no")]
    assert _should_fire_c5(trace_a) is True
    assert _should_fire_c5(trace_a, None) is True
    # Path (a) not satisfied → False since path (b) is also off.
    trace_b = [_entry("yes"), _entry("no")]
    assert _should_fire_c5(trace_b) is False
    assert _should_fire_c5(trace_b, None) is False


def test_c5_trigger_either_path_independently_sufficient() -> None:
    """Both paths (a) and (b) are alternatives — either one alone
    fires. When path (a) is satisfied AND convergence_reason is
    Option B, still True (no AND-ing)."""
    trace = [_entry("no"), _entry("no")]
    # Path (a) only.
    assert _should_fire_c5(trace, "hard_stop_scope_mismatch") is True
    # Path (b) only.
    trace_yes = [_entry("yes")]
    assert _should_fire_c5(trace_yes, "hard_stop_bridge_unreachable") is True
    # Both — still True.
    assert _should_fire_c5(trace, "hard_stop_bridge_unreachable") is True
