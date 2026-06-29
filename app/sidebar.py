"""Sidebar branding — compact block rendered at the TOP of the sidebar,
ABOVE the st.navigation menu. Called once from Home.py before pg.run().

Do NOT call from individual page scripts — that would double-render it
below the navigation.
"""
import streamlit as st
from pathlib import Path
import datetime


def render_sidebar_branding():
    """Compact branding block at the top of the sidebar."""
    # Honest: this is the synthetic data coverage window, not a fake "live refresh" timestamp.
    sample_period = "2024-01 to 2026-03"

    st.sidebar.markdown(
        f"""
        <div style='font-size:11px; line-height:1.55; padding:6px 4px 0 4px;
                    margin-bottom:0'>
          <div style='font-size:14px; font-weight:700; color:#e0e0e0; line-height:1.2'>
            📡 Logistics
          </div>
          <div style='color:#8892a4; margin-bottom:6px; font-size:10px; line-height:1.2'>
            Domain
          </div>
          <div style='color:#8892a4'>🗄️ <b>Sample data:</b> {sample_period}</div>
          <div style='color:#8892a4'>⚙️ <b>Stack:</b> DuckDB · dbt · DV 2.0</div>
          <div style='color:#8892a4'>📊 <b>Source:</b> SAP MM (sample)</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# Backwards-compat alias — no-op if called from a page (already rendered by Home.py).
def render_header():
    pass
