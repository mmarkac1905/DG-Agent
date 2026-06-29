"""Stage D.1 unit tests for app/_dar_render.py.

Smoke tests — validate that render_dar_card branches on analysis_type
and status without raising for all expected shapes, handles malformed
JSON + missing fields gracefully, and respects the _MAX_ROWS cap.

Uses a mock `st` object that records calls for inspection.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))

import _dar_render as render_mod  # noqa: E402


class _MockSt:
    """Minimal Streamlit stand-in. Records calls; never raises."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return record

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def call_args_for(self, name: str) -> list[tuple]:
        return [c[1] for c in self.calls if c[0] == name]


# ─── Per analysis_type × status shape tests ──────────────────────────

def _make_dar(analysis_type: str, status: str = "success",
              result_json: dict | None = None,
              query_sql: str = "SELECT 1") -> dict:
    rj = result_json if result_json is not None else {}
    return {
        "analysis_type": analysis_type,
        "status": status,
        "query_sql": query_sql,
        "result_json": json.dumps(rj),
        "executed_at_utc": "2026-04-23T12:00:00Z",
    }


def test_01_completeness_success_renders() -> None:
    st = _MockSt()
    dar = _make_dar("completeness", result_json={
        "column_checks": [
            {"column": "EBELN", "null_count": 0, "null_pct": 0.0,
             "reliability": "high"},
        ],
        "total_rows": 100,
    })
    render_mod.render_dar_card(dar, st)
    assert "code" in st.call_names()  # SQL rendered
    assert "dataframe" in st.call_names()  # results table rendered


def test_02_dimensions_success_renders() -> None:
    st = _MockSt()
    dar = _make_dar("dimensions", result_json={
        "columns_analyzed": [
            {
                "column_name": "BWART",
                "distinct_count": 4,
                "null_strategy": "none",
                "top_values": [
                    {"value": "201", "count": 100, "pct": 0.5},
                    {"value": "202", "count": 100, "pct": 0.5},
                ],
            },
        ],
    })
    render_mod.render_dar_card(dar, st)
    assert "dataframe" in st.call_names()


def test_03_magnitude_success_renders() -> None:
    st = _MockSt()
    dar = _make_dar("magnitude", result_json={
        "top_n": [
            {"dim_value": "A", "measure_total": 100, "row_count": 5},
        ],
        "total_rows": 1000,
        "measure_total_top_n": 100,
        "rationale": "top vendor by spend",
    })
    render_mod.render_dar_card(dar, st)
    assert "dataframe" in st.call_names()
    # Rationale section rendered
    markdown_args = [a[0] for a in st.call_args_for("markdown")]
    assert any("Rationale" in s for s in markdown_args)


def test_04_code_tables_success_renders() -> None:
    st = _MockSt()
    dar = _make_dar("code_tables", result_json={
        "mappings": [
            {"code": "201", "description": "Deploy",
             "description_source": "movement_type_mapping", "count": 100},
        ],
        "rationale": "BWART on MSEG",
    })
    render_mod.render_dar_card(dar, st)
    assert "dataframe" in st.call_names()


def test_05_date_success_renders() -> None:
    st = _MockSt()
    dar = _make_dar("temporal_coverage", result_json={
        "col_name": "BUDAT",
        "min": "2020-01-01",
        "max": "2026-04-23",
        "span_days": 2304,
        "null_pct": 0.0,
    })
    render_mod.render_dar_card(dar, st)
    # Key-value rendering (markdown calls)
    markdown_args = [a[0] for a in st.call_args_for("markdown")]
    assert any("col_name" in s for s in markdown_args)


def test_06_segmentation_success_renders() -> None:
    st = _MockSt()
    dar = _make_dar("segmentation_threshold", result_json={
        "col_name": "NETWR",
        "thresholds": [100, 500, 1000],
    })
    render_mod.render_dar_card(dar, st)
    # Rendered as key-value.


def test_07_grain_relationship_success_renders() -> None:
    st = _MockSt()
    dar = _make_dar("grain_relationship", result_json={
        "other_table": "ekpo",
        "role": "header",
        "sum_match_pct": 0.95,
        "confidence": "high",
    })
    render_mod.render_dar_card(dar, st)


def test_08_performance_baseline_success_renders() -> None:
    st = _MockSt()
    dar = _make_dar("performance_baseline", result_json={
        "col_name": "NETWR",
        "min": 10, "max": 10000, "avg": 500,
    })
    render_mod.render_dar_card(dar, st)


# ─── Skipped status ──────────────────────────────────────────────────

def test_09_skipped_dar_renders_skip_reason() -> None:
    st = _MockSt()
    dar = _make_dar("date", status="skipped", result_json={
        "skip_reason": "no date/timestamp columns in table",
        "blockers_addressed": [],
    })
    render_mod.render_dar_card(dar, st)
    info_args = [a[0] for a in st.call_args_for("info")]
    assert any("Skipped" in s for s in info_args)
    assert any("no date/timestamp" in s for s in info_args)


def test_10_skipped_no_reason_graceful() -> None:
    st = _MockSt()
    dar = _make_dar("date", status="skipped", result_json={})
    render_mod.render_dar_card(dar, st)
    # Doesn't crash; falls back to "(no reason provided)".


def test_10b_grain_relationship_skip_renders_topology_hint() -> None:
    """KI-116 — grain_relationship skips show the topology-context hint
    so analysts know it's expected (not a coverage gap)."""
    st = _MockSt()
    dar = _make_dar("grain_relationship", status="skipped", result_json={
        "skip_reason": "no shared numeric columns between tables",
    })
    render_mod.render_dar_card(dar, st)
    caption_args = [a[0] for a in st.call_args_for("caption")]
    captions_joined = " ".join(s for s in caption_args)
    assert "Why skipped" in captions_joined, (
        "KI-116 hint missing from grain_relationship skipped DAR"
    )
    assert "join_cardinality" in captions_joined
    assert "bridge_coverage_by_filter" in captions_joined


def test_10c_non_grain_relationship_skip_omits_topology_hint() -> None:
    """KI-116 hint must not appear for other analyzers' skips."""
    st = _MockSt()
    dar = _make_dar("date", status="skipped", result_json={
        "skip_reason": "no date/timestamp columns in table",
    })
    render_mod.render_dar_card(dar, st)
    caption_args = [a[0] for a in st.call_args_for("caption")]
    captions_joined = " ".join(s for s in caption_args)
    assert "Why skipped" not in captions_joined, (
        "KI-116 hint leaked to non-grain_relationship skipped DAR"
    )


# ─── Malformed / missing data ────────────────────────────────────────

def test_11_malformed_result_json_graceful() -> None:
    st = _MockSt()
    dar = {
        "analysis_type": "completeness",
        "status": "success",
        "query_sql": "SELECT 1",
        "result_json": "{not valid json",
        "executed_at_utc": "",
    }
    # Must not raise.
    render_mod.render_dar_card(dar, st)


def test_12_missing_query_sql_graceful() -> None:
    st = _MockSt()
    dar = {
        "analysis_type": "completeness",
        "status": "success",
        "query_sql": "",
        "result_json": json.dumps({"column_checks": []}),
        "executed_at_utc": "",
    }
    render_mod.render_dar_card(dar, st)
    # Doesn't crash; '(unavailable)' fallback rendered.


def test_13_unknown_analysis_type_falls_through() -> None:
    """Unknown analysis_type with deterministic status — falls through to
    key-value renderer (deterministic branch)."""
    st = _MockSt()
    dar = _make_dar("mystery_analyzer", result_json={"x": 1, "y": 2})
    render_mod.render_dar_card(dar, st)
    # Key-value rendered via markdown calls
    markdown_args = [a[0] for a in st.call_args_for("markdown")]
    assert any("x" in s for s in markdown_args)


def test_14_row_cap_respected() -> None:
    st = _MockSt()
    # 150 rows — should be capped at 100 and caption added.
    rows = [{"column": f"c{i}", "null_count": 0,
             "null_pct": 0.0, "reliability": "high"}
            for i in range(150)]
    dar = _make_dar("completeness", result_json={
        "column_checks": rows,
        "total_rows": 100,
    })
    render_mod.render_dar_card(dar, st)
    caption_args = [a[0] for a in st.call_args_for("caption")]
    assert any("50 rows not shown" in s for s in caption_args)


# ─── harness ────────────────────────────────────────────────────────

def _run_standalone() -> int:
    tests = [
        test_01_completeness_success_renders,
        test_02_dimensions_success_renders,
        test_03_magnitude_success_renders,
        test_04_code_tables_success_renders,
        test_05_date_success_renders,
        test_06_segmentation_success_renders,
        test_07_grain_relationship_success_renders,
        test_08_performance_baseline_success_renders,
        test_09_skipped_dar_renders_skip_reason,
        test_10_skipped_no_reason_graceful,
        test_11_malformed_result_json_graceful,
        test_12_missing_query_sql_graceful,
        test_13_unknown_analysis_type_falls_through,
        test_14_row_cap_respected,
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
