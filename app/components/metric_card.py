"""Reusable metric card with (i) info tooltip showing business glossary definition and S2T mapping.

Usage:
    from components.metric_card import metric_with_info
    metric_with_info("On-Time Delivery", "55.5%", term_id="BG002")
"""
import streamlit as st
import pandas as pd


@st.cache_data(ttl=300)
def _load_glossary():
    # Read via the in-memory Parquet-backed engine — never opens cpe_analytics.duckdb
    from db import query
    glossary = query("SELECT * FROM main_seeds.business_glossary")
    s2t = query("SELECT * FROM main_seeds.s2t_mapping")
    return glossary, s2t


def metric_with_info(
    label: str,
    value: str,
    term_id: str = None,
    delta: str = None,
    delta_color: str = "normal",
    help_text: str = None,
):
    """Display a metric with optional (i) tooltip from business glossary.

    If term_id is provided, the tooltip shows definition + sources + transformation.
    Otherwise falls back to help_text.
    """
    tooltip = None

    if term_id:
        try:
            glossary, s2t = _load_glossary()
            term = glossary[glossary['id'] == term_id]

            if not term.empty:
                t = term.iloc[0]
                term_s2t = s2t[s2t['business_term_id'] == term_id]

                lines = []
                lines.append(f"📊 {t['display_name']}")
                lines.append("━" * 40)
                lines.append(f"Definition: {t['definition']}")
                lines.append("")

                if not term_s2t.empty:
                    sources = term_s2t[['source_table', 'source_field']].drop_duplicates()
                    source_str = ", ".join(
                        [f"{r['source_table']}.{r['source_field']}" for _, r in sources.iterrows()]
                    )
                    lines.append(f"Sources: {source_str}")
                    lines.append("")

                    lines.append("Transformation:")
                    step = 1
                    for _, row in term_s2t.iterrows():
                        logic = row.get('transformation_logic_plain', '')
                        if pd.notna(logic) and str(logic).strip():
                            lines.append(f"  {step}. {logic}")
                            step += 1
                        join_desc = row.get('join_description', '')
                        if pd.notna(join_desc) and str(join_desc).strip():
                            lines.append(f"     ↳ Join: {join_desc}")
                        filter_desc = row.get('filter_description', '')
                        if pd.notna(filter_desc) and str(filter_desc).strip():
                            lines.append(f"     ↳ Filter: {filter_desc}")

                lines.append("")
                lines.append(f"Owner: {t.get('owner', 'N/A')}")
                status_icon = "✅" if t.get('status') == 'approved' else "📝"
                lines.append(f"Status: {str(t.get('status', 'unknown')).upper()} {status_icon}")

                tooltip = "\n".join(lines)
        except Exception as e:
            tooltip = help_text or f"(metric_with_info error: {e})"

    if tooltip is None:
        tooltip = help_text

    st.metric(
        label=label,
        value=value,
        delta=delta,
        delta_color=delta_color,
        help=tooltip,
    )
