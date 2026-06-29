"""Piece 9 Stage D.1 — Term EDA prerequisite check (run-all canonical).

Per v4 D5 + v5 Edit 5 + Stage D.1 §28.11.8:

  - Term status must be in the allowed set: {scope_confirmed,
    domain_eda_pending, term_eda_pending, ready_for_s2t}.

  - For each scope table, every canonical analyzer must have a DAR
    with status IN ('success', 'skipped'). Term-agnostic filter: any
    success/skipped DAR on a given table from any context satisfies.

  - Canonical analyzer set (8 types):
      completeness, dimensions, magnitude, code_tables, date,
      segmentation, grain_relationship, performance_baseline

  - Special cases:
      * grain_relationship is PAIRWISE. A table is satisfied iff either
        (a) scope has only 1 table (auto-satisfied), OR
        (b) every unordered pair containing this table has a success/
            skipped DAR with source_tables='<sorted,pair>'.
      * performance_baseline is co-emitted with magnitude. Satisfied
        iff magnitude DAR exists with status IN ('success', 'skipped').

Response shape (Stage D.1):
    {
        "ready": bool,
        "term_status_ok": bool,
        "current_status": str,
        "scope_tables": list[str],             # all scope tables, ordered
        "missing_analyzers_per_table": {       # only tables with gaps
            "<table>": ["<analyzer>", ...],
        },
        "missing_grain_pairs": list[(str, str)],  # sorted pairs missing DAR
        "reason": "ready" | "term_not_found" | "term_status_invalid"
                | "scope_empty" | "analyzer_coverage_incomplete",
        "next_steps": list[str],
    }

next_steps rebuild:
  - One entry per scope table with any missing per-table analyzer.
  - One entry for missing grain pairs (if any).
  - Bounded by scope_table_count + 1.
"""
from __future__ import annotations

import itertools
from typing import Optional

import duckdb


_ALLOWED_STATUSES = frozenset({
    "scope_confirmed", "domain_eda_pending",
    "term_eda_pending", "ready_for_s2t",
})

_PER_TABLE_ANALYZERS = (
    "completeness", "dimensions", "magnitude", "code_tables",
    "date", "segmentation",
)
# performance_baseline is auto-satisfied by magnitude; checked implicitly.
# grain_relationship is pairwise; checked separately via pair logic.

# known_issue #77: _PER_TABLE_ANALYZERS keeps UI-facing labels so next_steps
# output matches the Domain Analysis tab's display names. The DAR storage
# label differs for two analyzers — run_date_analysis writes
# analysis_type='temporal_coverage' and run_segmentation_analysis writes
# 'segmentation_threshold'. Translate UI → storage for SQL lookup only.
_DAR_STORAGE_LABEL: dict[str, str] = {
    "date": "temporal_coverage",
    "segmentation": "segmentation_threshold",
}

_SATISFYING_DAR_STATUSES = frozenset({"success", "skipped"})


def check_term_eda_prereq(
    conn: duckdb.DuckDBPyConnection,
    term_id: str,
) -> dict:
    """Verify a term is ready for Stage C execution. See module docstring."""
    result: dict = {
        "ready": False,
        "term_status_ok": False,
        "current_status": "",
        "scope_tables": [],
        "missing_analyzers_per_table": {},
        "missing_grain_pairs": [],
        "reason": "",
        "next_steps": [],
    }

    # 1. Term exists + status check.
    row = conn.execute(
        "SELECT status FROM main_seeds.business_glossary WHERE id = ?",
        [term_id],
    ).fetchone()
    if not row:
        result["reason"] = "term_not_found"
        result["next_steps"] = [
            f"Term '{term_id}' does not exist in business_glossary."
        ]
        return result

    current_status = (row[0] or "").strip()
    result["current_status"] = current_status
    if current_status not in _ALLOWED_STATUSES:
        result["reason"] = "term_status_invalid"
        result["next_steps"] = [
            f"Term status is '{current_status}'. Stage C requires one of "
            f"{sorted(_ALLOWED_STATUSES)}. "
            "Run Stage A (Term Scope tab) to confirm scope first."
        ]
        return result
    result["term_status_ok"] = True

    # 2. Scope resolution.
    scope_rows = conn.execute(
        "SELECT DISTINCT source_table FROM main_seeds.s2t_mapping "
        "WHERE business_term_id = ? ORDER BY 1",
        [term_id],
    ).fetchall()
    scope_tables = [r[0].lower() for r in scope_rows if r[0]]
    result["scope_tables"] = scope_tables
    if not scope_tables:
        result["reason"] = "scope_empty"
        result["next_steps"] = [
            f"Term '{term_id}' has no s2t_mapping rows (scope is empty). "
            "Confirm scope via Term Scope tab first."
        ]
        return result

    # 3. Per-table analyzer coverage (6 per-table types; magnitude
    # satisfies performance_baseline implicitly).
    missing_per_table: dict[str, list[str]] = {}
    for t in scope_tables:
        missing_for_this_table: list[str] = []
        for analyzer in _PER_TABLE_ANALYZERS:
            # #77: translate UI label → DAR storage label for SQL lookup.
            storage_label = _DAR_STORAGE_LABEL.get(analyzer, analyzer)
            n = conn.execute(
                "SELECT COUNT(*) FROM main_seeds.domain_analysis_results "
                "WHERE LOWER(source_tables) = LOWER(?) "
                "AND analysis_type = ? "
                "AND status IN ('success', 'skipped')",
                [t, storage_label],
            ).fetchone()[0]
            if not n:
                missing_for_this_table.append(analyzer)

        # performance_baseline auto-satisfied iff magnitude exists
        # with status IN ('success', 'skipped'). Already counted by
        # the magnitude check above. We still report missing
        # performance_baseline in the grid output when magnitude is
        # missing — label it explicitly so the analyst understands
        # the single-fix dependency.
        if "magnitude" in missing_for_this_table:
            missing_for_this_table.append("performance_baseline")

        # grain_relationship per-table check: auto-satisfied for
        # single-table scope; otherwise checked via pair logic below.
        # Per-table grid entry for grain_relationship: satisfied iff
        # every pair containing this table has coverage.
        if len(scope_tables) == 1:
            # single-table scope — grain_relationship auto-satisfied
            pass
        else:
            # Check if all pairs containing this table have a DAR.
            pair_missing = False
            for other in scope_tables:
                if other == t:
                    continue
                pair_sorted = ",".join(sorted([t, other]))
                n_pair = conn.execute(
                    "SELECT COUNT(*) FROM main_seeds.domain_analysis_results "
                    "WHERE source_tables = ? "
                    "AND analysis_type = 'grain_relationship' "
                    "AND status IN ('success', 'skipped')",
                    [pair_sorted],
                ).fetchone()[0]
                if not n_pair:
                    pair_missing = True
                    break
            if pair_missing:
                missing_for_this_table.append("grain_relationship")

        if missing_for_this_table:
            missing_per_table[t] = missing_for_this_table

    # 4. Global grain_relationship pair check (single source of truth
    # for the missing_grain_pairs output field).
    missing_grain_pairs: list[tuple[str, str]] = []
    if len(scope_tables) >= 2:
        for t1, t2 in itertools.combinations(sorted(scope_tables), 2):
            pair_sorted = ",".join([t1, t2])  # already sorted
            n_pair = conn.execute(
                "SELECT COUNT(*) FROM main_seeds.domain_analysis_results "
                "WHERE source_tables = ? "
                "AND analysis_type = 'grain_relationship' "
                "AND status IN ('success', 'skipped')",
                [pair_sorted],
            ).fetchone()[0]
            if not n_pair:
                missing_grain_pairs.append((t1, t2))
    result["missing_grain_pairs"] = missing_grain_pairs
    result["missing_analyzers_per_table"] = missing_per_table

    # 5. Build next_steps bounded by scope_table_count + 1.
    next_steps: list[str] = []
    for t in scope_tables:
        if t in missing_per_table:
            missing_list = ", ".join(missing_per_table[t])
            next_steps.append(
                f"Run {missing_list} on {t} via Domain Analysis tab"
            )
    if missing_grain_pairs:
        next_steps.append(
            f"Run Grain Relationships on All Pairs via Business Term "
            f"Analysis tab for this term ({len(missing_grain_pairs)} "
            f"pair(s) missing)"
        )

    if missing_per_table or missing_grain_pairs:
        result["reason"] = "analyzer_coverage_incomplete"
        result["next_steps"] = next_steps
        return result

    # All checks pass.
    result["ready"] = True
    result["reason"] = "ready"
    result["next_steps"] = [
        "Prerequisites met. Run Term EDA to begin Stage C trajectory."
    ]
    return result
