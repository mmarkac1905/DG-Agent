"""Stage D.2 unit tests for app/_s2t_tab_helpers.py.

Covers four layers:
  1. is_s2t_eligible — pure dual-path gate. 6 tests.
  2. has_piece8_s2t_rows — Piece 8 Deploy discriminator. 3 tests.
  3. status_to_stage_index + get_s2t_action — action dispatch. 11 tests.
  4. render_* helpers via _MockSt (Stage D.1 pattern). 6 tests.

Data fetchers (`_get_prereq`, `_get_latest_tar_sufficiency`,
`_has_analysis_findings`) are monkeypatched for get_s2t_action tests —
we don't want the in-memory DuckDB + parquet stack spun up in unit
tests, and the fetchers are already exercised end-to-end in the manual
smoke test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))

import _s2t_tab_helpers as mod  # noqa: E402


class _MockSt:
    """Stage D.1 _MockSt clone. Records calls via __getattr__; never raises.

    For columns(n), returns a list of _MockSt children so the per-column
    markdown calls are recorded on child objects (mirrors st.columns
    behavior of returning context-manager-like column handles)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.children: list["_MockSt"] = []

    def __getattr__(self, name: str):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "columns":
                n = args[0] if args else 1
                self.children = [_MockSt() for _ in range(n)]
                return self.children
            return None
        return record

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def all_markdown_calls(self) -> list[str]:
        out = [c[1][0] for c in self.calls if c[0] == "markdown" and c[1]]
        for child in self.children:
            out.extend(child.all_markdown_calls())
        return out


# ─── is_s2t_eligible ───────────────────────────────────────────────────

def test_01_archived_hard_stop() -> None:
    term = {"status": "archived"}
    eligible, reason = mod.is_s2t_eligible(term, has_legacy_findings=True)
    assert eligible is False
    assert reason == "archived_hard_stop"


def test_02_ready_for_s2t_no_legacy_eligible() -> None:
    term = {"status": "ready_for_s2t"}
    eligible, reason = mod.is_s2t_eligible(term, has_legacy_findings=False)
    assert eligible is True
    assert reason == "eligible"


def test_03_approved_no_legacy_eligible() -> None:
    term = {"status": "approved"}
    eligible, reason = mod.is_s2t_eligible(term, has_legacy_findings=False)
    assert eligible is True


def test_04_draft_no_legacy_ineligible() -> None:
    term = {"status": "draft"}
    eligible, reason = mod.is_s2t_eligible(term, has_legacy_findings=False)
    assert eligible is False
    assert reason == "ineligible_status_draft"


def test_05_scope_confirmed_no_legacy_ineligible() -> None:
    term = {"status": "scope_confirmed"}
    eligible, reason = mod.is_s2t_eligible(term, has_legacy_findings=False)
    assert eligible is False
    assert reason == "ineligible_status_scope_confirmed"


def test_06_draft_plus_legacy_eligible() -> None:
    term = {"status": "draft"}
    eligible, reason = mod.is_s2t_eligible(term, has_legacy_findings=True)
    assert eligible is True
    assert reason == "eligible"


# ─── has_piece8_s2t_rows ───────────────────────────────────────────────

def test_06a_piece8_empty_df_returns_false() -> None:
    df = pd.DataFrame(columns=['id', 'target_model'])
    assert mod.has_piece8_s2t_rows(df) is False


def test_06b_piece8_stage_a_only_returns_false() -> None:
    # Stage A shape: target_model is None / NaN (varies how pandas reads it).
    df = pd.DataFrame([
        {'id': 'S2T-0001', 'target_model': None},
        {'id': 'S2T-0002', 'target_model': ''},
        {'id': 'S2T-0003', 'target_model': float('nan')},
    ])
    assert mod.has_piece8_s2t_rows(df) is False


def test_06c_piece8_with_deploy_row_returns_true() -> None:
    # Mixed: 2 Stage A rows + 1 Deploy row → should return True.
    df = pd.DataFrame([
        {'id': 'S2T-0001', 'target_model': None},
        {'id': 'S2T-0002', 'target_model': ''},
        {'id': 'S2T-0003', 'target_model': 'fact_cpe_deployments'},
    ])
    assert mod.has_piece8_s2t_rows(df) is True


# ─── status_to_stage_index ─────────────────────────────────────────────

def test_07_status_to_stage_index_known() -> None:
    assert mod.status_to_stage_index("draft") == 0
    assert mod.status_to_stage_index("scope_confirmed") == 2
    assert mod.status_to_stage_index("term_eda_pending") == 3
    assert mod.status_to_stage_index("ready_for_s2t") == 4
    assert mod.status_to_stage_index("approved") == 5
    assert mod.status_to_stage_index("denied") == 5
    assert mod.status_to_stage_index("archived") == -1


def test_08_status_to_stage_index_unknown() -> None:
    assert mod.status_to_stage_index("nonexistent") == -1
    assert mod.status_to_stage_index("") == -1


# ─── get_s2t_action ────────────────────────────────────────────────────

def _stub_prereq(mod_, scope_tables, missing_map=None, missing_pairs=None):
    """Replace _get_prereq with a fake that returns a fixed shape."""
    def fake(term_id):
        return {
            "ready": not (missing_map or missing_pairs),
            "scope_tables": scope_tables,
            "missing_analyzers_per_table": missing_map or {},
            "missing_grain_pairs": missing_pairs or [],
            "reason": "",
            "next_steps": [],
        }
    mod_._get_prereq = fake


def _stub_tar(mod_, payload):
    mod_._get_latest_tar_sufficiency = lambda term_id: payload


def test_09_draft_action_shape() -> None:
    a = mod.get_s2t_action("draft", "BG001", has_piece8_mapping=False, glossary_row={})
    assert a["show_create_button"] is False
    assert a["details_key"] is None
    assert a["deep_link_hint"] is not None  # "Switch to Term Detail..."
    assert a["deep_link_target"] is None
    assert "definition" in a["action_text"].lower()


def test_10_scope_confirmed_no_dars(monkeypatch) -> None:
    _stub_prereq(mod, ["mseg", "equi"], missing_map={
        "mseg": ["completeness", "dimensions"],
        "equi": ["completeness"],
    })
    a = mod.get_s2t_action("scope_confirmed", "BG001", has_piece8_mapping=False, glossary_row={})
    assert a["show_create_button"] is False
    assert a["details_key"] == "scope_coverage"
    assert "Next: run Domain EDA" in a["action_text"]
    assert a["deep_link_target"] == "pages/Data_Analysis.py"
    assert a["deep_link_label"] == "Go to Domain Analysis"


def test_11_scope_confirmed_full_coverage(monkeypatch) -> None:
    _stub_prereq(mod, ["mseg"], missing_map={}, missing_pairs=[])
    a = mod.get_s2t_action("scope_confirmed", "BG001", has_piece8_mapping=False, glossary_row={})
    assert a["details_key"] == "scope_coverage"
    assert "Domain EDA complete" in a["action_text"]
    assert a["deep_link_label"] == "Go to Business Term Analysis"


def test_12_term_eda_pending(monkeypatch) -> None:
    _stub_prereq(mod, ["mseg"])
    _stub_tar(mod, None)
    a = mod.get_s2t_action("term_eda_pending", "BG001", has_piece8_mapping=False, glossary_row={})
    assert a["show_create_button"] is False
    assert a["details_key"] == "tar_summary"
    assert a["deep_link_target"] == "pages/Data_Analysis.py"


def test_13_ready_for_s2t_without_mapping(monkeypatch) -> None:
    """BG027-like: term at ready_for_s2t with Stage A rows only (or empty).
    Create S2T button should render."""
    _stub_prereq(mod, ["mseg"])
    _stub_tar(mod, {
        "confidence": "high",
        "declared_sufficient": True,
        "lens_consideration": {},
        "blockers_resolution": [],
        "run_id": "RUN-X",
        "executed_at_utc": "2026-04-23T00:00:00Z",
    })
    a = mod.get_s2t_action(
        "ready_for_s2t", "BG001",
        has_piece8_mapping=False, glossary_row={},
    )
    assert a["show_create_button"] is True
    assert a["details_key"] == "tar_summary"
    assert "Ready for S2T" in a["action_text"]


def test_14_ready_for_s2t_with_mapping(monkeypatch) -> None:
    """Post-Deploy, pre-approval: Create button hidden, awaiting approval hint."""
    a = mod.get_s2t_action(
        "ready_for_s2t", "BG001",
        has_piece8_mapping=True, glossary_row={},
    )
    assert a["show_create_button"] is False
    assert a["details_key"] == "awaiting_approval"
    assert a["deep_link_hint"] is not None


def test_15_approved_without_mapping(monkeypatch) -> None:
    """Approved via tab_detail before Deploy — still show Create button."""
    _stub_prereq(mod, ["mseg"])
    _stub_tar(mod, None)
    a = mod.get_s2t_action(
        "approved", "BG001",
        has_piece8_mapping=False, glossary_row={},
    )
    assert a["show_create_button"] is True


def test_16_approved_with_mapping(monkeypatch) -> None:
    """Approved + deployed: done state, Re-run deferred."""
    a = mod.get_s2t_action(
        "approved", "BG001",
        has_piece8_mapping=True, glossary_row={},
    )
    assert a["show_create_button"] is False
    assert a["note"] is not None
    assert "Re-run" in a["note"]


def test_17_denied_action() -> None:
    a = mod.get_s2t_action(
        "denied", "BG001",
        has_piece8_mapping=False,
        glossary_row={"notes": "Some context | DENIED by Alice: wrong grain"},
    )
    assert a["show_create_button"] is False
    assert a["details_key"] == "denial_info"
    assert a["details_data"].get("notes_excerpt") is not None


def test_18_archived_action() -> None:
    a = mod.get_s2t_action(
        "archived", "BG001",
        has_piece8_mapping=False,
        glossary_row={
            "archive_id": "ARC-20260401-001",
            "archived_at_utc": "2026-04-01T12:00:00Z",
            "archived_reason_code": "obsolete",
            "archived_reason_text": "Superseded by BG050",
        },
    )
    assert a["show_create_button"] is False
    assert a["details_key"] == "archive_info"
    assert a["details_data"]["archive_id"] == "ARC-20260401-001"


def test_19_unknown_status_safe_fallback() -> None:
    a = mod.get_s2t_action("mystery", "BG001", has_piece8_mapping=False, glossary_row={})
    assert a["show_create_button"] is False
    assert "mystery" in a["action_text"]


# ─── render_status_badge / render_pipeline_strip / render_details_panel ─

def test_20_render_status_badge_emits_markdown() -> None:
    st = _MockSt()
    mod.render_status_badge("approved", st)
    names = st.call_names()
    assert "markdown" in names
    md = st.all_markdown_calls()
    assert any("APPROVED" in m for m in md)


def test_21_render_pipeline_strip_six_columns() -> None:
    st = _MockSt()
    mod.render_pipeline_strip("scope_confirmed", st)
    # columns(6) was called once with 6
    cols_calls = [c for c in st.calls if c[0] == "columns"]
    assert len(cols_calls) == 1
    assert cols_calls[0][1] == (6,)
    # Each child column had exactly one markdown call (6 total)
    total_markdown = sum(
        sum(1 for c in child.calls if c[0] == "markdown")
        for child in st.children
    )
    assert total_markdown == 6


def test_22_render_pipeline_strip_denied_has_x_marker() -> None:
    st = _MockSt()
    mod.render_pipeline_strip("denied", st)
    md = st.all_markdown_calls()
    assert any("✗" in m for m in md), "denied should render ✗ on current stage"


def test_23_render_details_panel_none_is_noop() -> None:
    st = _MockSt()
    mod.render_details_panel(None, {}, st)
    assert st.calls == []


def test_24_render_details_panel_archive_info() -> None:
    st = _MockSt()
    mod.render_details_panel("archive_info", {
        "archive_id": "ARC-X",
        "archived_at_utc": "2026-01-01",
        "archived_reason_code": "obsolete",
        "archived_reason_text": "",
    }, st)
    assert "warning" in st.call_names()


def test_25_render_details_panel_denial_info_with_excerpt() -> None:
    st = _MockSt()
    mod.render_details_panel("denial_info", {"notes_excerpt": "DENIED by X: reason"}, st)
    assert "warning" in st.call_names()
    assert "text" in st.call_names()


# ─── known_issue #83 — extract_trailing_digits ─────────────────────────

def test_extract_trailing_digits_handles_mixed_id_schemes() -> None:
    """Regression for known_issue #83.

    Deploy S2T step's max-ID finder previously used
    `x.replace('S', '')` + int(). That strips every 'S' (not just
    leading) and crashed on the modern S2T-NNNN scheme:
      'S2T-0001'.replace('S', '') = '2T-0001'
      int('2T-0001') → ValueError
    Fix: regex-based trailing-digit extraction via
    extract_trailing_digits.

    This test encodes the three real ID schemes currently coexisting
    in s2t_mapping.csv + verifies the max is picked correctly across
    them + new IDs generated in the modern S2T-NNNN format don't
    collide with any existing ID.
    """
    # Representative sample mirroring today's s2t_mapping.csv patterns.
    existing_ids = pd.Series([
        "S001",       # legacy SNNN (numeric = 1)
        "S037",       # legacy SNNN (numeric = 37)
        "S045",       # legacy SNNN (numeric = 45) — global max today
        "S2T-0001",   # modern S2T-NNNN (numeric = 1) — the crash trigger
        "S2T-0005",   # modern S2T-NNNN (numeric = 5)
        "BG028-01",   # term-prefixed (numeric = 1)
        "BG028-07",   # term-prefixed (numeric = 7)
    ])

    # Parser picks trailing digits regardless of prefix scheme.
    extracted = existing_ids.apply(mod.extract_trailing_digits).tolist()
    assert extracted == [1, 37, 45, 1, 5, 1, 7], (
        f"trailing-digit extraction wrong; got {extracted}"
    )

    # Max across the full mixed set: 45 (from S045).
    max_num = int(existing_ids.apply(mod.extract_trailing_digits).max() or 0)
    assert max_num == 45, f"expected max=45; got {max_num}"

    # Simulate the deploy's new-row ID generation: 3 new rows starting
    # at max+1, formatted in the modern S2T-NNNN scheme.
    new_ids = [f"S2T-{max_num + i + 1:04d}" for i in range(3)]
    assert new_ids == ["S2T-0046", "S2T-0047", "S2T-0048"]

    # New IDs must not collide with any existing ID — numerical
    # separation guarantees this regardless of scheme.
    existing_set = set(existing_ids)
    for nid in new_ids:
        assert nid not in existing_set, (
            f"collision: new ID {nid!r} already exists in the CSV"
        )


# ─── known_issue #84 — classify_dbt_error ──────────────────────────────

def test_classify_dbt_error_timeout_opts_out_of_retry() -> None:
    """Timeouts never benefit from retry — LLM can't fix a hung query."""
    for sample in ("Timed out after 5 minutes", "TimeoutExpired: ..."):
        result = mod.classify_dbt_error(sample)
        assert result["should_retry"] is False, (
            f"timeout should not retry; sample={sample!r} result={result}"
        )
        assert result["hint"] == ""


def test_classify_dbt_error_empty_yields_generic_retry() -> None:
    """Empty / whitespace-only error → retry with generic hint (no
    specifics to say)."""
    for sample in ("", "   ", "\n\n"):
        result = mod.classify_dbt_error(sample)
        assert result["should_retry"] is True
        assert "schema" in result["hint"].lower() or "error" in result["hint"].lower()
        assert result["failed_col"] is None


def test_classify_dbt_error_binder_column_not_found() -> None:
    """Phase 13's original case. Preserves column extraction +
    candidates for back-compat with existing log lines + schema-dump
    targeting."""
    sample = (
        'Binder Error: Referenced column "material_description" not '
        'found in FROM clause!\nCandidate bindings: "material_number", '
        '"model_description", "manufacturer"\n'
        '...\nFailure in model fact_cpe (models/marts/fact_cpe.sql)'
    )
    result = mod.classify_dbt_error(sample)
    assert result["should_retry"] is True
    assert result["failed_col"] == "material_description"
    assert "material_number" in result["candidates"]
    assert "model_description" in result["candidates"]
    assert "manufacturer" in result["candidates"]
    assert result["failed_model"] == "fact_cpe"
    assert "column" in result["hint"].lower()


def test_classify_dbt_error_catalog_table_not_found() -> None:
    """ref() points to a non-existent table."""
    sample = (
        "CatalogException: Table with name fact_missing does not exist!\n"
        "Did you mean fact_purchase_orders?"
    )
    result = mod.classify_dbt_error(sample)
    assert result["should_retry"] is True
    assert result["failed_col"] is None
    assert "ref" in result["hint"].lower() or "table" in result["hint"].lower()


def test_classify_dbt_error_syntax() -> None:
    """Parser / syntax error — retry with syntax hint."""
    sample = "Parser Error: syntax error at or near 'FROM'"
    result = mod.classify_dbt_error(sample)
    assert result["should_retry"] is True
    assert "syntax" in result["hint"].lower()


def test_classify_dbt_error_type_mismatch() -> None:
    """Conversion / type error — retry with CAST hint."""
    samples = [
        "Conversion Error: Could not convert string 'abc' to INTEGER",
        "Binder Error: Type mismatch on column X",
    ]
    for sample in samples:
        result = mod.classify_dbt_error(sample)
        assert result["should_retry"] is True
        assert "type" in result["hint"].lower() or "cast" in result["hint"].lower()


def test_classify_dbt_error_io_spill_is_todays_bg027_case() -> None:
    """Regression guard for today's BG027 failure. The IO-error
    classifier is the key unlock — pre-#84 this would have been
    rejected by the Binder-only gate.

    The cardinality hint must steer the LLM toward JOIN-key inspection
    (vs just 'fix the temp-dir config'). Assert the hint mentions
    cartesian / JOIN / cardinality concepts explicitly so the LLM sees
    the directional pointer.
    """
    sample = (
        'DuckDB adapter: duckdb error: IO Error: Failed to create '
        'directory "\\\\.tmp": The specified path is invalid.\n'
        'Failure in model fact_active_deployed_cpe '
        '(models/marts/fact_active_deployed_cpe.sql)'
    )
    result = mod.classify_dbt_error(sample)
    assert result["should_retry"] is True, (
        "IO errors must retry post-#84 (today's BG027 class)"
    )
    assert result["failed_model"] == "fact_active_deployed_cpe"
    hint_lower = result["hint"].lower()
    assert "join" in hint_lower, (
        f"spill hint must mention JOIN keys — got: {result['hint']!r}"
    )
    assert any(
        term in hint_lower
        for term in ("cartesian", "cardinality", "selectivity", "many rows")
    ), f"spill hint must flag cardinality risk — got: {result['hint']!r}"


def test_classify_dbt_error_unknown_falls_back_to_generic_retry() -> None:
    """Any error text we don't specifically recognize still retries
    with a generic hint (vs Phase 13's hard break on unknown)."""
    sample = "Something went wrong. Vague dbt output that matches no class."
    result = mod.classify_dbt_error(sample)
    assert result["should_retry"] is True
    assert result["hint"]  # non-empty


def test_extract_trailing_digits_handles_non_string_and_digitless() -> None:
    """Edge cases — NaN / None / empty / no-trailing-digits all
    map to 0 so `.apply(...).max()` stays finite on pathological
    CSVs."""
    assert mod.extract_trailing_digits(None) == 0
    assert mod.extract_trailing_digits(float("nan")) == 0
    assert mod.extract_trailing_digits("") == 0
    assert mod.extract_trailing_digits("no_digits_here") == 0
    assert mod.extract_trailing_digits("S") == 0
    assert mod.extract_trailing_digits("123") == 123  # pure numeric string
    assert mod.extract_trailing_digits("abc123def") == 0  # digits not trailing


# ─── harness ───────────────────────────────────────────────────────────

def _run_standalone() -> int:
    tests = [
        test_01_archived_hard_stop,
        test_02_ready_for_s2t_no_legacy_eligible,
        test_03_approved_no_legacy_eligible,
        test_04_draft_no_legacy_ineligible,
        test_05_scope_confirmed_no_legacy_ineligible,
        test_06_draft_plus_legacy_eligible,
        test_06a_piece8_empty_df_returns_false,
        test_06b_piece8_stage_a_only_returns_false,
        test_06c_piece8_with_deploy_row_returns_true,
        test_07_status_to_stage_index_known,
        test_08_status_to_stage_index_unknown,
        test_09_draft_action_shape,
        test_10_scope_confirmed_no_dars,
        test_11_scope_confirmed_full_coverage,
        test_12_term_eda_pending,
        test_13_ready_for_s2t_without_mapping,
        test_14_ready_for_s2t_with_mapping,
        test_15_approved_without_mapping,
        test_16_approved_with_mapping,
        test_17_denied_action,
        test_18_archived_action,
        test_19_unknown_status_safe_fallback,
        test_20_render_status_badge_emits_markdown,
        test_21_render_pipeline_strip_six_columns,
        test_22_render_pipeline_strip_denied_has_x_marker,
        test_23_render_details_panel_none_is_noop,
        test_24_render_details_panel_archive_info,
        test_25_render_details_panel_denial_info_with_excerpt,
    ]
    failed = 0
    for t in tests:
        try:
            # Some tests take an unused `monkeypatch` parameter to match the
            # spec wording; we don't use pytest's fixture system in the
            # standalone harness — the _stub_* helpers mutate module state
            # directly. Pass None for the fixture arg.
            if t.__code__.co_argcount == 1:
                t(None)
            else:
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
