"""
CPE Procurement Analytics — Streamlit Entry Point
Helios Telecom · Data Product MVP

Uses st.navigation with sections: Dashboard · Data Governance.
"""
import streamlit as st

st.set_page_config(
    page_title="CPE Procurement Analytics — Helios Telecom",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
    .stApp { background-color: #0a0e17; font-size: 14px; }

    /* Global font-size tightening — pages were rendering too large and forcing
       users to zoom the browser to 80%. These overrides keep proportions right
       at 100% browser zoom. */
    .stMetric label { font-size: 12px !important; }
    .stMetric [data-testid="stMetricValue"] { font-size: 24px !important; }
    h1 { font-size: 28px !important; }
    h2 { font-size: 22px !important; }
    h3 { font-size: 18px !important; }
    .stTabs [data-baseweb="tab"] {
        font-size: 13px !important;
        padding: 6px 12px !important;
    }
    section[data-testid="stSidebar"] .stMarkdown { font-size: 13px !important; }

    .metric-card {
        background-color: #131a2e; border: 1px solid #1e2a45;
        border-radius: 8px; padding: 16px; text-align: center;
    }
    .metric-value { font-size: 28px; font-weight: 700; color: #e0e0e0; }
    .metric-label { font-size: 12px; color: #8892a4; text-transform: uppercase; letter-spacing: 1px; }
    .metric-delta-good { color: #4ade80; font-size: 13px; }
    .metric-delta-bad { color: #f87171; font-size: 13px; }
    section[data-testid="stSidebar"] { background-color: #0d1220; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] {
        background-color: #131a2e; border-radius: 6px;
    }
    /* Reorder sidebar so UserContent (branding) appears ABOVE the nav menu */
    section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {
        display: flex !important;
        flex-direction: column !important;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarHeader"] { order: 1; }
    section[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
        order: 2;
        padding-top: 0 !important;
        padding-bottom: 30px !important;  /* gives 24px text-to-text gap, matches Dashboard↔Data Governance section spacing measured via Range API */
        margin-bottom: 0 !important;
    }
    section[data-testid="stSidebar"] [data-testid="stSidebarNav"] {
        order: 3;
        margin-top: 0 !important;
        padding-top: 0 !important;
    }
</style>
""", unsafe_allow_html=True)

from sidebar import render_sidebar_branding
render_sidebar_branding()

dashboard_pages = [
    st.Page("pages/Procurement_Overview.py", title="Procurement Overview", icon="📦", default=True),
    st.Page("pages/Vendor_Scorecard.py",     title="Vendor Scorecard",     icon="🏢"),
    st.Page("pages/CPE_Lifecycle.py",        title="CPE Lifecycle",        icon="📱"),
    st.Page("pages/Inventory_Health.py",     title="Inventory Health",     icon="📊"),
    st.Page("pages/Goods_Movements.py",      title="Goods Movements",      icon="🔄"),
]

governance_pages = [
    st.Page("pages/Business_Glossary.py", title="Business Glossary", icon="📖"),
    st.Page("pages/Data_Analysis.py",     title="Data Analysis",     icon="🔬"),
    st.Page("pages/Data_Model.py",        title="Data Model",        icon="🗂️"),
    st.Page("pages/Data_Catalog.py",      title="Data Catalog",      icon="🔗"),
]

documentation_pages = [
    st.Page("pages/Wiki_Pages.py",     title="Wiki Pages",     icon="📖"),
    st.Page("pages/Seeds_Catalog.py",  title="Seeds Catalog",  icon="🌱"),
]

pg = st.navigation({
    "Dashboard":       dashboard_pages,
    "Data Governance": governance_pages,
    "Documentation":   documentation_pages,
})
pg.run()
