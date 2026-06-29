"""Term Analysis Result section renderer (C5 closure 3/4).

Displays the latest business_term_analysis_results (BAR) row for a
selected term inside the Business Glossary's Detail tab. Special
treatment for status=needs_data_extension surfaces the C5 sourcing
recommendations + the Option B gate's reachability violations so
the analyst can act on a structurally-unanswerable term.

Per tasks/c5_design.md Component 5. ~150 LOC; pure rendering, no
state mutation, no LLM calls.

Public entry point: render_bar_section(term_id, query).
"""
from __future__ import annotations

import json
from typing import Any, Callable, Optional

import streamlit as st


# BAR.status palette — distinct from business_glossary.status palette
# (workflow state) so analysts don't conflate the two dimensions.
_BAR_STATUS_VISUALS: dict[str, dict[str, str]] = {
    "needs_data_extension": {
        "color": "#f59e0b",  # amber — needs analyst intervention, not broken
        "icon": "⚠️",
        "label": "NEEDS DATA EXTENSION",
        "tone": "warning",
    },
    "converged": {
        "color": "#10b981",  # green
        "icon": "✅",
        "label": "CONVERGED",
        "tone": "success",
    },
    "hard_stop": {
        "color": "#ef4444",  # red
        "icon": "🛑",
        "label": "HARD STOP",
        "tone": "error",
    },
    "failed": {
        "color": "#dc2626",  # darker red
        "icon": "❌",
        "label": "FAILED",
        "tone": "error",
    },
}

_GRADE_VISUALS: dict[str, dict[str, str]] = {
    "verified": {"color": "#10b981", "label": "verified"},
    "verified_low_priority": {"color": "#86efac",
                              "label": "verified · low priority"},
    "divergence_warning": {"color": "#f59e0b",
                           "label": "divergence warning"},
    "unverified": {"color": "#9ca3af", "label": "unverified"},
    "scope_review_needed": {"color": "#9ca3af",
                            "label": "scope review needed"},
}

_TIER_ORDER = ("primary", "hypothesis", "customer_namespace")
_TIER_LABELS = {
    "primary": "Primary candidates",
    "hypothesis": "Hypothesis candidates",
    "customer_namespace": "Customer-namespace (Z*) candidates",
}


def _safe_load_json(blob: Any) -> Any:
    """Tolerant JSON parse — returns None on empty / null / parse error.
    BAR JSON columns occasionally contain Python None, '', or already-
    parsed dict/list when read via pandas; normalize all to a parseable
    Python value or None."""
    if blob is None or blob == "" or blob == "null":
        return None
    if isinstance(blob, (dict, list)):
        return blob
    try:
        return json.loads(blob)
    except Exception:
        return None


def _status_visuals(bar_status: Optional[str]) -> dict[str, str]:
    fallback = {"color": "#6b7280", "icon": "❔",
                "label": (bar_status or "UNKNOWN").upper(), "tone": "info"}
    return _BAR_STATUS_VISUALS.get(bar_status or "", fallback)


def _grade_visuals(grade: Optional[str]) -> dict[str, str]:
    fallback = {"color": "#6b7280", "label": grade or "—"}
    return _GRADE_VISUALS.get(grade or "", fallback)


def _render_status_header(bar: dict) -> None:
    vis = _status_visuals(bar.get("status"))
    st.markdown(
        f"### {vis['icon']} <span style='color:{vis['color']};"
        f"font-weight:bold'>{vis['label']}</span>",
        unsafe_allow_html=True,
    )
    cr = bar.get("convergence_reason") or "—"
    bar_id = bar.get("id") or "—"
    cost = bar.get("llm_total_cost_usd")
    iters = bar.get("iterations_count")
    cost_str = f"${cost:.3f}" if isinstance(cost, (int, float)) else "—"
    st.caption(
        f"BAR `{bar_id}` · convergence_reason: `{cr}` · "
        f"iterations: {iters if iters is not None else '—'} · "
        f"total cost: {cost_str}"
    )


def _render_recommendations(bar: dict) -> None:
    sr = _safe_load_json(bar.get("sourcing_recommendations"))
    if not sr:
        skipped = bar.get("c5_skipped_reason")
        if skipped:
            st.info(
                f"C5 sourcing recommendations were not produced "
                f"(reason: `{skipped}`)."
            )
        return

    summary = sr.get("summary", {}) if isinstance(sr, dict) else {}
    recs = sr.get("validated_recommendations", []) if isinstance(sr, dict) else []

    total = summary.get("total_recommendations", len(recs))
    a = summary.get("case_a_count", 0)
    b = summary.get("case_b_count", 0)
    c = summary.get("case_c_count", 0)
    d = summary.get("case_d_count", 0)
    st.markdown(
        f"**Sourcing recommendations** — {total} total "
        f"(verified: {a} · already-in-scope: {b} · "
        f"divergence_warning: {c} · no-match: {d})"
    )

    by_tier: dict[str, list[dict]] = {t: [] for t in _TIER_ORDER}
    for r in recs:
        tier = (r.get("tier") or "hypothesis").lower()
        by_tier.setdefault(tier, []).append(r)

    for tier in _TIER_ORDER:
        items = by_tier.get(tier) or []
        if not items:
            continue
        st.markdown(f"##### {_TIER_LABELS.get(tier, tier.title())}")
        for r in items:
            _render_recommendation_card(r)
    extra = [t for t in by_tier if t not in _TIER_ORDER and by_tier[t]]
    for tier in extra:
        st.markdown(f"##### Tier: {tier}")
        for r in by_tier[tier]:
            _render_recommendation_card(r)


def _render_recommendation_card(r: dict) -> None:
    table = r.get("table_name") or "—"
    grade = r.get("recommendation_grade") or r.get("grade") or "unverified"
    gvis = _grade_visuals(grade)
    join_keys = r.get("join_keys") or []
    if isinstance(join_keys, list):
        join_keys_str = ", ".join(join_keys) if join_keys else "—"
    else:
        join_keys_str = str(join_keys)
    rationale = r.get("rationale") or ""
    catalog_desc = (r.get("catalog_description")
                    or r.get("brief_description") or "")
    confidence = r.get("confidence_grade") or r.get("llm_confidence") or ""

    head = (
        f"<div style='border-left:4px solid {gvis['color']};"
        f"padding:8px 12px;margin:6px 0;background:rgba(0,0,0,0.02)'>"
        f"<div><b style='font-size:1.05em'>{table}</b> "
        f"<span style='background:{gvis['color']};color:white;"
        f"padding:2px 8px;border-radius:10px;font-size:0.75em;"
        f"margin-left:8px'>{gvis['label']}</span>"
        f"{f' <span style=\"color:#6b7280;font-size:0.85em\">· LLM '
            f'confidence: {confidence}</span>' if confidence else ''}</div>"
        f"<div style='color:#374151;font-size:0.9em;margin-top:4px'>"
        f"<b>Join keys:</b> <code>{join_keys_str}</code></div>"
    )
    if rationale:
        head += (
            f"<div style='margin-top:6px;font-size:0.92em'>"
            f"{rationale}</div>"
        )
    if catalog_desc:
        head += (
            f"<div style='margin-top:4px;color:#6b7280;font-size:0.85em;"
            f"font-style:italic'>Catalog: {catalog_desc}</div>"
        )
    head += "</div>"
    st.markdown(head, unsafe_allow_html=True)

    extras: list[tuple[str, Any]] = []
    if r.get("validation_source"):
        extras.append(("validation_source", r["validation_source"]))
    if r.get("seed_columns"):
        extras.append(("seed_columns", r["seed_columns"]))
    if r.get("catalog_key_fields"):
        extras.append(("catalog_key_fields", r["catalog_key_fields"]))
    if extras:
        with st.expander(f"More on {table}", expanded=False):
            for k, v in extras:
                st.markdown(f"- **{k}:** `{v}`")


def _render_reachability_violations(bar: dict) -> None:
    cr = bar.get("convergence_reason") or ""
    if cr not in ("hard_stop_bridge_unreachable",
                  "hard_stop_bridge_attestation_missing"):
        return
    trace = _safe_load_json(bar.get("iteration_trace")) or []
    if not trace:
        return
    last = trace[-1] if isinstance(trace, list) and trace else {}
    gates = (last.get("gates_result") or {}) if isinstance(last, dict) else {}
    if cr == "hard_stop_bridge_unreachable":
        violations = gates.get("bridge_violations") or []
    else:
        single = gates.get("violation") or ""
        violations = [single] if single else []
    if not violations:
        return
    with st.expander(
        "Why these recommendations? (data-side gate violations)",
        expanded=True,
    ):
        st.markdown(
            "The system's data-side gate refused the proposed SQL because "
            "the chosen join paths cannot reach the values the term filters on:"
        )
        for v in violations:
            st.markdown(f"- {v}")
        st.caption(
            "Each violation cites a `bridge_coverage_by_filter` DAR — "
            "empirical evidence from seeded data — so the refusal is "
            "deterministic, not LLM judgment."
        )


def _render_bridge_coverage_consulted(bar: dict) -> None:
    consulted = _safe_load_json(bar.get("bridge_coverage_consulted")) or []
    if not consulted:
        return
    with st.expander(
        f"Bridge-coverage DARs the LLM consulted ({len(consulted)})",
        expanded=False,
    ):
        for cid in consulted:
            st.markdown(f"- `{cid}`")
        st.caption(
            "These are the bridge_coverage_by_filter DARs the iteration LLM "
            "echoed in its attestation — evidence the analyst can audit "
            "to verify the LLM read the available reachability data before "
            "producing SQL."
        )


def _fetch_bar_row(
    term_id: str,
    query: Callable,
    bar_id: Optional[str] = None,
) -> Optional[dict]:
    """Resolve the BAR row to render.

    When `bar_id` is provided, bypass the parquet-backed `query` callable
    and read directly from cpe_analytics.duckdb (mirrors _bar_consumer's
    direct-conn pattern). This makes the refusal-path renderer wire to
    the dispatcher's id rather than parquet's latest — robust against
    parquet staleness (KI-103).

    When `bar_id` is None, use the existing parquet-backed latest-by-time
    fallback.
    """
    if bar_id is not None:
        import duckdb
        from pathlib import Path
        db_path = Path(__file__).resolve().parent.parent / "cpe_analytics.duckdb"
        with duckdb.connect(str(db_path), read_only=True) as conn:
            df = conn.execute(
                "SELECT * FROM main_seeds.business_term_analysis_results "
                "WHERE id = ?",
                [bar_id],
            ).df()
        if df is None or df.empty:
            return None
        return df.iloc[0].to_dict()

    safe_id = str(term_id).replace("'", "''")
    df = query(
        f"SELECT * FROM main_seeds.business_term_analysis_results "
        f"WHERE business_term_id = '{safe_id}' "
        f"ORDER BY executed_at_utc DESC NULLS LAST, id DESC "
        f"LIMIT 1"
    )
    if df is None or df.empty:
        return None
    return df.iloc[0].to_dict()


def render_bar_section(
    term_id: str,
    query: Callable,
    bar_id: Optional[str] = None,
) -> None:
    """Public entry — render the Term Analysis Result section.

    `query` is the page's parameterized DuckDB query helper (returns a
    pandas DataFrame). When `bar_id` is provided (e.g., from the C6
    dispatcher refusal output), the renderer bypasses `query` and reads
    that exact BAR from live cpe_analytics.duckdb — keeps the rendered
    BAR aligned with the dispatcher's decision regardless of parquet
    staleness. When `bar_id` is None, falls back to latest-by-time via
    `query`.
    """
    st.subheader("📊 Term Analysis Result")
    st.caption(
        "Latest result of the LLM-driven SQL iteration loop (Piece 8). "
        "Status reflects analysis outcome; if the system couldn't answer "
        "the term from the confirmed scope, sourcing recommendations show "
        "which additional tables would close the gap."
    )

    bar = _fetch_bar_row(term_id, query, bar_id=bar_id)
    if bar is None:
        if bar_id is not None:
            st.error(
                f"BAR `{bar_id}` not found in cpe_analytics.duckdb "
                f"(referenced by Stage E refusal but missing from "
                f"main_seeds.business_term_analysis_results). Data "
                f"integrity issue — investigate dispatcher source."
            )
            return
        st.info(
            "No analysis result yet. Run the Piece 8 iteration loop to "
            "produce one (`python scripts/run_term_injection.py "
            f"--term-id {term_id}`)."
        )
        return

    _render_status_header(bar)
    _render_reachability_violations(bar)
    _render_recommendations(bar)
    _render_bridge_coverage_consulted(bar)
