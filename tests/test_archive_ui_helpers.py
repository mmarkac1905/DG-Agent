"""KI #71 Step 3 unit tests — pure helpers in _archive_ui.

Streamlit widget calls aren't exercised here; tests cover the session-
state state machine + the chain formatter that drives the unwind UX.

Test plan
---------
01  _start_unwind initialises target + empty resolved list
02  _start_unwind is idempotent for same target
03  _record_unwind_step appends de-duplicated term_ids
04  _record_unwind_step ignores the target itself
05  _record_unwind_step is a no-op when no unwind in flight
06  _clear_unwind wipes both keys
07  _is_unwind_target distinguishes terminal from intermediate
08  _format_chain handles empty + multi-step chains
09  _get_resolved_list returns [] when missing or wrong type
10  module imports cleanly (smoke — no Streamlit context required)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))


def _fresh_state() -> dict:
    """Stand-in for st.session_state — supports __getitem__/__setitem__/
    setdefault/pop/get/__contains__. A plain dict satisfies every call
    site in _archive_ui's pure helpers."""
    return {}


def test_01_start_unwind_initialises_target_and_resolved() -> None:
    from _archive_ui import (
        UNWIND_RESOLVED_KEY,
        UNWIND_TARGET_KEY,
        _start_unwind,
    )
    state = _fresh_state()
    _start_unwind(state, "BG011")
    assert state[UNWIND_TARGET_KEY] == "BG011"
    assert state[UNWIND_RESOLVED_KEY] == []


def test_02_start_unwind_is_idempotent_for_same_target() -> None:
    from _archive_ui import (
        UNWIND_RESOLVED_KEY,
        _record_unwind_step,
        _start_unwind,
    )
    state = _fresh_state()
    _start_unwind(state, "BG011")
    _record_unwind_step(state, "BG017")
    # Calling _start_unwind again with the SAME target must NOT wipe
    # the in-flight resolved list.
    _start_unwind(state, "BG011")
    assert state[UNWIND_RESOLVED_KEY] == ["BG017"]


def test_03_record_unwind_step_appends_dedup() -> None:
    from _archive_ui import UNWIND_RESOLVED_KEY, _record_unwind_step, _start_unwind
    state = _fresh_state()
    _start_unwind(state, "BG011")
    _record_unwind_step(state, "BG017")
    _record_unwind_step(state, "BG020")
    _record_unwind_step(state, "BG017")  # duplicate
    assert state[UNWIND_RESOLVED_KEY] == ["BG017", "BG020"]


def test_04_record_unwind_step_ignores_the_target() -> None:
    """Target's archive does not append itself — that's the
    terminating event, recorded separately on archive_log."""
    from _archive_ui import UNWIND_RESOLVED_KEY, _record_unwind_step, _start_unwind
    state = _fresh_state()
    _start_unwind(state, "BG011")
    _record_unwind_step(state, "BG011")
    assert state[UNWIND_RESOLVED_KEY] == []


def test_05_record_unwind_step_no_op_when_no_unwind() -> None:
    from _archive_ui import _record_unwind_step
    state = _fresh_state()
    _record_unwind_step(state, "BG017")
    # No keys set, no exceptions raised.
    assert state == {}


def test_06_clear_unwind_wipes_both_keys() -> None:
    from _archive_ui import (
        UNWIND_RESOLVED_KEY,
        UNWIND_TARGET_KEY,
        _clear_unwind,
        _record_unwind_step,
        _start_unwind,
    )
    state = _fresh_state()
    _start_unwind(state, "BG011")
    _record_unwind_step(state, "BG017")
    assert UNWIND_TARGET_KEY in state
    assert UNWIND_RESOLVED_KEY in state
    _clear_unwind(state)
    assert UNWIND_TARGET_KEY not in state
    assert UNWIND_RESOLVED_KEY not in state


def test_07_is_unwind_target_distinguishes_terminal_from_intermediate() -> None:
    from _archive_ui import (
        _is_unwind_target,
        _is_unwinding,
        _start_unwind,
    )
    state = _fresh_state()
    assert _is_unwinding(state) is False
    _start_unwind(state, "BG011")
    assert _is_unwinding(state) is True
    assert _is_unwind_target(state, "BG011") is True
    assert _is_unwind_target(state, "BG017") is False


def test_08_format_chain_renders_chain() -> None:
    from _archive_ui import _format_chain
    gloss = pd.DataFrame([
        {"id": "BG011", "term_name": "total_purchase_orders"},
        {"id": "BG017", "term_name": "vendor_concentration_risk"},
    ])
    assert _format_chain([], gloss) == "(none yet)"
    out = _format_chain(["BG017", "BG011"], gloss)
    assert "BG017" in out and "vendor_concentration_risk" in out
    assert "BG011" in out and "total_purchase_orders" in out
    # Unknown ids fall back to '?' but don't crash.
    out2 = _format_chain(["BG999"], gloss)
    assert "BG999" in out2 and "?" in out2


def test_09_get_resolved_list_returns_empty_on_missing_or_wrong_type() -> None:
    from _archive_ui import _get_resolved_list
    assert _get_resolved_list({}) == []
    assert _get_resolved_list({"archive_unwind_resolved": None}) == []
    assert _get_resolved_list({"archive_unwind_resolved": "not a list"}) == []
    assert _get_resolved_list({"archive_unwind_resolved": ["a", "b"]}) == ["a", "b"]


def test_10_module_imports_cleanly() -> None:
    """Smoke: every import in _archive_ui resolves without a running
    Streamlit script context."""
    import _archive_ui  # noqa: F401
    assert hasattr(_archive_ui, "render_archive_section")
    assert hasattr(_archive_ui, "_navigate_to_term")
    assert hasattr(_archive_ui, "_execute_archive")


def _run_standalone() -> int:
    tests = [
        test_01_start_unwind_initialises_target_and_resolved,
        test_02_start_unwind_is_idempotent_for_same_target,
        test_03_record_unwind_step_appends_dedup,
        test_04_record_unwind_step_ignores_the_target,
        test_05_record_unwind_step_no_op_when_no_unwind,
        test_06_clear_unwind_wipes_both_keys,
        test_07_is_unwind_target_distinguishes_terminal_from_intermediate,
        test_08_format_chain_renders_chain,
        test_09_get_resolved_list_returns_empty_on_missing_or_wrong_type,
        test_10_module_imports_cleanly,
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
