"""Tests for app/_prereq_dispatch.py — flattening prereq output
into a unified subprocess dispatch list."""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "app"))

from _prereq_dispatch import (  # noqa: E402
    _DETERMINISTIC_ANALYZER_COST_USD,
    _LLM_ANALYZER_COST_USD,
    compute_missing_dispatch_items,
)


def test_compute_missing_dispatch_items_empty():
    prereq = {
        "missing_analyzers_per_table": {},
        "missing_grain_pairs": [],
    }
    assert compute_missing_dispatch_items(prereq) == []


def test_compute_missing_dispatch_items_per_table_only():
    prereq = {
        "missing_analyzers_per_table": {
            "mkpf": ["code_tables"],
            "mseg": ["code_tables"],
        },
        "missing_grain_pairs": [],
    }
    items = compute_missing_dispatch_items(prereq)
    assert len(items) == 2
    assert all(it["kind"] == "per_table" for it in items)
    assert all(it["analyzer"] == "code_tables" for it in items)
    assert all(
        it["script_rel"] == "scripts/run_code_tables_analysis.py"
        for it in items
    )
    assert all(not it["is_deterministic"] for it in items)
    assert all(
        it["est_cost_usd"] == _LLM_ANALYZER_COST_USD for it in items
    )
    targets = sorted(it["target_label"] for it in items)
    assert targets == ["mkpf", "mseg"]
    # args shape for singular analyzer
    for it in items:
        assert it["args"][0] == "--table"
        assert it["args"][1] == it["target_label"]


def test_compute_missing_dispatch_items_per_pair_only():
    prereq = {
        "missing_analyzers_per_table": {},
        "missing_grain_pairs": [("equi", "seri"), ("mkpf", "seri")],
    }
    items = compute_missing_dispatch_items(prereq)
    assert len(items) == 2
    assert all(it["kind"] == "per_pair" for it in items)
    assert all(it["analyzer"] == "grain_relationship" for it in items)
    assert all(it["is_deterministic"] for it in items)
    assert all(
        it["est_cost_usd"] == _DETERMINISTIC_ANALYZER_COST_USD
        for it in items
    )
    # args shape: --pairs "<t1>,<t2>"
    for it in items:
        assert it["args"][0] == "--pairs"
        assert "," in it["args"][1]
    targets = sorted(it["target_label"] for it in items)
    assert targets == ["equi,seri", "mkpf,seri"]


def test_compute_missing_dispatch_items_mixed_BG027_pattern():
    """BG027's exact prereq state. Asserts grain_relationship dedup
    (4 per-pair items, NOT 5 per-table grain entries).
    """
    prereq = {
        "missing_analyzers_per_table": {
            "equi": ["grain_relationship"],
            "mkpf": ["code_tables", "grain_relationship"],
            "mseg": ["code_tables", "grain_relationship"],
            "objk": [
                "magnitude", "performance_baseline", "grain_relationship",
            ],
            "seri": ["grain_relationship"],
        },
        "missing_grain_pairs": [
            ("equi", "seri"), ("mkpf", "seri"),
            ("mseg", "seri"), ("objk", "seri"),
        ],
    }
    items = compute_missing_dispatch_items(prereq)

    # 4 grain pairs + 2 code_tables + 1 magnitude = 7 total.
    # NOT 5 per-table grain (skipped) + 4 per-pair = 9.
    assert len(items) == 7

    by_analyzer: dict = {}
    for it in items:
        by_analyzer[it["analyzer"]] = by_analyzer.get(it["analyzer"], 0) + 1
    assert by_analyzer == {
        "grain_relationship": 4,
        "code_tables": 2,
        "magnitude": 1,
    }

    # All grain_relationship items must be per_pair, never per_table.
    grain_items = [it for it in items if it["analyzer"] == "grain_relationship"]
    assert all(it["kind"] == "per_pair" for it in grain_items)

    # performance_baseline must NOT appear as a dispatch.
    assert not any(it["analyzer"] == "performance_baseline" for it in items)


def test_compute_missing_dispatch_items_deterministic_first_ordering():
    prereq = {
        "missing_analyzers_per_table": {
            "mseg": ["code_tables"],
            "objk": ["magnitude"],
        },
        "missing_grain_pairs": [("equi", "seri")],
    }
    items = compute_missing_dispatch_items(prereq)

    # Deterministic items first, then LLM items.
    flags = [it["is_deterministic"] for it in items]
    assert flags == [True, False, False]

    # Within LLM group, sorted by (analyzer, target_label).
    llm_items = [it for it in items if not it["is_deterministic"]]
    llm_keys = [(it["analyzer"], it["target_label"]) for it in llm_items]
    assert llm_keys == sorted(llm_keys)


def test_compute_missing_dispatch_items_cost_aggregation():
    """Sum of est_cost_usd matches expected total for a mixed batch."""
    prereq = {
        "missing_analyzers_per_table": {
            "mkpf": ["code_tables"],
            "mseg": ["code_tables"],
            "objk": ["magnitude"],
        },
        "missing_grain_pairs": [
            ("equi", "seri"), ("mkpf", "seri"),
        ],
    }
    items = compute_missing_dispatch_items(prereq)
    total_cost = sum(it["est_cost_usd"] for it in items)
    # 3 LLM × $0.05 + 2 deterministic × $0 = $0.15
    assert abs(total_cost - 0.15) < 1e-9
    # Latency aggregation: 3×60 + 2×30 = 240s
    total_max_seconds = sum(it["est_seconds_max"] for it in items)
    assert total_max_seconds == 240
