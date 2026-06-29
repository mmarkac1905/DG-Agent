"""Prerequisite dispatch helpers — pure functions for translating
`_term_eda_prereq` output into a unified subprocess dispatch list.

Consumed by the Business Term Analysis tab's "Run All Prerequisites"
button. The Streamlit-bound handler that consumes these items lives
in `app/pages/Data_Analysis.py`; this module is intentionally
pure-Python so it is unit-testable without a Streamlit context.

Two non-obvious dispatch rules:

  - `grain_relationship` is dispatched per-pair, not per-table. The
    prereq function flags `grain_relationship` on every table whose
    pairs are incomplete, but each missing-pair run satisfies all
    implicated per-table flags simultaneously. Emitting per-table
    grain items would over-count.

  - `performance_baseline` is co-emitted by `magnitude` — there is
    no separate script. Skip it from per-table dispatch.
"""
from __future__ import annotations

_LLM_ANALYZER_COST_USD: float = 0.05
_LLM_ANALYZER_LATENCY_MAX_S: int = 60
_DETERMINISTIC_ANALYZER_COST_USD: float = 0.0
_DETERMINISTIC_ANALYZER_LATENCY_MAX_S: int = 30

# Per-table analyzers that fire LLM calls. Confirmed via grep of
# `scripts/run_*_analysis.py` for `claude_api` / `messages.create`.
# Mirrors the "singular" arg_flavor in `app/_analyzer_registry.py`.
_LLM_ANALYZERS: frozenset = frozenset({
    "completeness", "dimensions", "magnitude", "code_tables",
})

# UI label → (script_filename, arg_flavor). Keyed by UI label
# (which is what `_term_eda_prereq` emits in
# `missing_analyzers_per_table`). Storage labels (`temporal_coverage`,
# `segmentation_threshold`) are translated by the prereq function
# before reaching this map.
_ANALYZER_TO_SCRIPT: dict = {
    "completeness":  ("run_completeness_analysis.py",   "singular"),
    "dimensions":    ("run_dimensions_analysis.py",     "singular"),
    "magnitude":     ("run_magnitude_analysis.py",      "singular"),
    "code_tables":   ("run_code_tables_analysis.py",    "singular"),
    "date":          ("run_date_analysis.py",           "plural"),
    "segmentation":  ("run_segmentation_analysis.py",   "plural"),
}

# Labels that prereq emits but are NOT directly dispatchable.
_PREREQ_ANALYZERS_NOT_DISPATCHED: frozenset = frozenset({
    "grain_relationship", "performance_baseline",
})


def compute_missing_dispatch_items(prereq: dict) -> list:
    """Flatten prereq output into a unified dispatch list.

    Each item shape:
        {
            "kind": "per_table" | "per_pair",
            "analyzer": str,
            "target_label": str,
            "script_rel": str,           # "scripts/<file>.py"
            "args": list[str],           # e.g., ["--table", "mkpf"]
            "est_cost_usd": float,
            "est_seconds_max": int,
            "is_deterministic": bool,
        }

    Sort: deterministic-first, then by analyzer name, then
    target_label.
    """
    items: list = []

    # Per-table items (skip grain_relationship + performance_baseline).
    missing_per_table = prereq.get("missing_analyzers_per_table") or {}
    for table, analyzer_list in missing_per_table.items():
        for analyzer in analyzer_list:
            if analyzer in _PREREQ_ANALYZERS_NOT_DISPATCHED:
                continue
            script_info = _ANALYZER_TO_SCRIPT.get(analyzer)
            if script_info is None:
                # Unknown label — skip defensively.
                continue
            script_rel, arg_flavor = script_info
            arg_key = "--tables" if arg_flavor == "plural" else "--table"
            is_det = analyzer not in _LLM_ANALYZERS
            items.append({
                "kind": "per_table",
                "analyzer": analyzer,
                "target_label": table,
                "script_rel": f"scripts/{script_rel}",
                "args": [arg_key, table],
                "est_cost_usd": (
                    _DETERMINISTIC_ANALYZER_COST_USD if is_det
                    else _LLM_ANALYZER_COST_USD
                ),
                "est_seconds_max": (
                    _DETERMINISTIC_ANALYZER_LATENCY_MAX_S if is_det
                    else _LLM_ANALYZER_LATENCY_MAX_S
                ),
                "is_deterministic": is_det,
            })

    # Per-pair items (always grain_relationship, always deterministic).
    missing_pairs = prereq.get("missing_grain_pairs") or []
    for pair in missing_pairs:
        t1, t2 = pair[0], pair[1]
        target_label = f"{t1},{t2}"
        items.append({
            "kind": "per_pair",
            "analyzer": "grain_relationship",
            "target_label": target_label,
            "script_rel": "scripts/run_grain_relationship_analysis.py",
            "args": ["--pairs", target_label],
            "est_cost_usd": _DETERMINISTIC_ANALYZER_COST_USD,
            "est_seconds_max": _DETERMINISTIC_ANALYZER_LATENCY_MAX_S,
            "is_deterministic": True,
        })

    items.sort(key=lambda it: (
        not it["is_deterministic"],  # False sorts before True → det first
        it["analyzer"],
        it["target_label"],
    ))
    return items
