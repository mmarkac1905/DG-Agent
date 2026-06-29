"""Stage D.1 — shared DAR rendering helpers for collapsible cards.

Used by both Stage B Section 1 (Domain Analysis tab — per-analyzer
collapsible cell) and Stage C's grain-relationship subsection
(Business Term Analysis tab — per-pair collapsible card).

Branches on (analysis_type, status):
  - LLM analyzers (completeness, dimensions, magnitude, code_tables)
    + status='success'/'error' → render SQL + structured results +
    rationale.
  - Deterministic analyzers (date, segmentation, grain_relationship,
    performance_baseline) + status='success'/'error' → render
    placeholder SQL comment + key-value finding dict.
  - status='skipped' (any analyzer) → render skip_reason banner.
  - Malformed result_json → render "(unavailable)" inline; never raise.

Row cap for any rendered list: 100 rows. If exceeded, show first 100
with caption "(N more rows not shown — inspect CSV directly for full
data)". Caps prevent hanging the browser on DARs with thousands of
values (e.g. dimensions result with high-cardinality column).
"""
from __future__ import annotations

import json
from typing import Any

import pandas as pd


LLM_ANALYZERS = frozenset({
    "completeness", "dimensions", "magnitude", "code_tables",
})

DETERMINISTIC_ANALYZERS = frozenset({
    "date", "segmentation", "grain_relationship", "performance_baseline",
    "temporal_coverage",  # run_date_analysis emits with this analysis_type
    "segmentation_threshold",  # run_segmentation_analysis emits with this
})

_MAX_ROWS = 100


def render_dar_card(dar_row: dict, st) -> None:
    """Top-level render function. Branches on status then analysis_type.

    Parameters
    ----------
    dar_row : dict
        A DAR row as dict (from DictReader). Must have 'analysis_type'
        and 'status' keys; others optional.
    st : streamlit module
        Passed in so render helpers can call st.code(), st.dataframe()
        etc. without importing streamlit (lets the module be unit-tested
        with a mock `st`).
    """
    status = (dar_row.get("status") or "unknown").strip()
    if status == "skipped":
        _render_skipped_dar(dar_row, st)
        return

    analysis_type = (dar_row.get("analysis_type") or "").strip()
    if analysis_type in LLM_ANALYZERS:
        _render_llm_analyzer_dar(dar_row, st)
    else:
        _render_deterministic_analyzer_dar(dar_row, st)


def _render_skipped_dar(dar_row: dict, st) -> None:
    try:
        rj = json.loads(dar_row.get("result_json") or "{}")
        reason = rj.get("skip_reason", "(no reason provided)")
    except (json.JSONDecodeError, TypeError):
        reason = "(malformed result_json)"
    st.info(f"**Skipped:** {reason}")

    # KI-116 — surface analyzer-specific context so analysts don't read
    # an expected skip as a coverage gap. grain_relationship is for
    # measure-comparison between tables that share numeric columns;
    # header-line and dimension-fact pairs (typical SAP MM topology)
    # legitimately skip and are covered by other analyzers.
    if (dar_row.get("analysis_type") or "").strip() == "grain_relationship":
        st.caption(
            "**Why skipped:** `grain_relationship` compares measures "
            "between tables that share numeric columns. Header-line and "
            "dimension-fact pairs (typical for SAP MM topology) are "
            "covered by `join_cardinality` and `bridge_coverage_by_filter` "
            "instead — this skip is expected behavior, not a coverage gap."
        )

    if dar_row.get("executed_at_utc"):
        st.caption(f"Run at: {dar_row['executed_at_utc']}")


def _render_llm_analyzer_dar(dar_row: dict, st) -> None:
    """SQL + structured results + rationale."""
    # SQL
    sql = dar_row.get("query_sql") or ""
    if sql:
        st.markdown("**Query SQL**")
        st.code(sql, language="sql")
    else:
        st.markdown("**Query SQL**: _(unavailable)_")

    # Results
    st.markdown("**Results**")
    _render_result_json_table(dar_row, st)

    # Rationale + auxiliary LLM-decision fields
    _render_rationale_if_present(dar_row, st)


def _render_deterministic_analyzer_dar(dar_row: dict, st) -> None:
    """Placeholder SQL comment + key-value result_json."""
    sql = dar_row.get("query_sql") or ""
    if sql:
        st.markdown("**Query SQL**")
        st.code(sql, language="sql")
        st.caption(
            "Deterministic analyzer — query_sql is a placeholder comment. "
            "See the analyzer script source for the full SQL template."
        )
    st.markdown("**Results**")
    _render_result_json_keyvalue(dar_row, st)


def _parse_result_json(dar_row: dict) -> dict | None:
    raw = dar_row.get("result_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _render_result_json_table(dar_row: dict, st) -> None:
    rj = _parse_result_json(dar_row)
    if rj is None:
        st.warning("result_json is unavailable or malformed")
        return

    atype = (dar_row.get("analysis_type") or "").strip()

    if atype == "completeness":
        rows = rj.get("column_checks") or []
        _render_capped_table(rows, st)
        if rj.get("total_rows") is not None:
            st.caption(f"Table total rows: {rj['total_rows']}")
    elif atype == "dimensions":
        cols_analyzed = rj.get("columns_analyzed") or []
        for col_entry in cols_analyzed[:_MAX_ROWS]:
            if not isinstance(col_entry, dict):
                continue
            col_name = col_entry.get("column_name", "?")
            distinct = col_entry.get("distinct_count", "?")
            null_strategy = col_entry.get("null_strategy", "?")
            st.markdown(
                f"**Column: `{col_name}`** "
                f"(distinct={distinct}, null_strategy={null_strategy})"
            )
            top_vals = col_entry.get("top_values") or []
            _render_capped_table(top_vals, st)
        if len(cols_analyzed) > _MAX_ROWS:
            st.caption(
                f"(+{len(cols_analyzed) - _MAX_ROWS} more columns not shown)"
            )
    elif atype == "magnitude":
        rows = rj.get("top_n") or []
        _render_capped_table(rows, st)
        total_rows = rj.get("total_rows")
        top_n_sum = rj.get("measure_total_top_n")
        caption_parts = []
        if total_rows is not None:
            caption_parts.append(f"table total rows: {total_rows}")
        if top_n_sum is not None:
            caption_parts.append(f"top-N measure sum: {top_n_sum}")
        if caption_parts:
            st.caption("; ".join(caption_parts))
    elif atype == "code_tables":
        rows = rj.get("mappings") or []
        _render_capped_table(rows, st)
        if rj.get("distinct_sources_in_output"):
            st.caption(
                f"Description sources: {rj['distinct_sources_in_output']}"
            )
    else:
        # Fallback: show as key-value.
        _render_result_json_keyvalue(dar_row, st)


def _render_capped_table(rows: list, st) -> None:
    if not rows:
        st.caption("_(no rows)_")
        return
    try:
        df = pd.DataFrame(rows[:_MAX_ROWS])
        st.dataframe(df, hide_index=True)
    except Exception as e:  # noqa: BLE001
        st.warning(f"Could not render result table: {type(e).__name__}: {e}")
        return
    if len(rows) > _MAX_ROWS:
        st.caption(
            f"(+{len(rows) - _MAX_ROWS} rows not shown — inspect "
            f"domain_analysis_results.csv for full data)"
        )


def _render_result_json_keyvalue(dar_row: dict, st) -> None:
    rj = _parse_result_json(dar_row)
    if rj is None:
        st.warning("result_json is unavailable or malformed")
        return
    # Sort keys for deterministic display, but hoist 'skip_reason' to
    # the top when present.
    priority_keys = ["skip_reason", "col_name", "grouping_dimension",
                     "measure", "other_table", "role"]
    rendered_keys: set[str] = set()
    for k in priority_keys:
        if k in rj:
            _render_key_value(k, rj[k], st)
            rendered_keys.add(k)
    for k in sorted(rj.keys()):
        if k in rendered_keys:
            continue
        if k in ("blockers_addressed", "blockers_contract_violation",
                 "blockers_contract_violation_reason"):
            continue  # Stage B fields — rendered in a dedicated panel elsewhere
        _render_key_value(k, rj[k], st)


def _render_key_value(k: str, v: Any, st) -> None:
    """Render one key-value pair. Pretty-print lists + dicts."""
    if isinstance(v, (list, tuple)) and len(v) > 10:
        st.markdown(f"**{k}**: _(list of {len(v)} items)_")
        try:
            df = pd.DataFrame(v[:_MAX_ROWS])
            st.dataframe(df, hide_index=True)
        except Exception:  # noqa: BLE001
            st.markdown(f"`{v[:10]} …`")
    elif isinstance(v, dict) and len(v) > 0:
        st.markdown(f"**{k}**:")
        for sub_k, sub_v in v.items():
            st.markdown(f"- **{sub_k}**: `{sub_v}`")
    else:
        st.markdown(f"**{k}**: `{v}`")


def _render_rationale_if_present(dar_row: dict, st) -> None:
    rj = _parse_result_json(dar_row)
    if rj is None:
        return
    rationale = rj.get("rationale")
    if rationale:
        st.markdown("**Rationale**")
        st.markdown(rationale)
    # Auxiliary LLM-decision fields (surface each if present).
    aux_fields = [
        "columns_chosen_by_llm", "null_strategy_per_column",
        "measure_chosen", "dimension_chosen", "shape_claimed",
        "description_source_used", "description_source_reason",
        "used_join_not_case", "measure_source",  # Stage D.1 telemetry
    ]
    shown_any = False
    for field in aux_fields:
        if field in rj:
            if not shown_any:
                st.markdown("**LLM decisions:**")
                shown_any = True
            st.markdown(f"- **{field}**: `{rj[field]}`")
