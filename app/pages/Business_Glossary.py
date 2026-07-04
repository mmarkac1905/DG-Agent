"""Business Glossary — business term definitions, S2T mapping, profiling, approval workflow.

Two audiences, two tabs:
  - 🔍 Term Detail         — business view (plain language, no jargon)
  - 🔧 S2T Specification   — data analyst view (full technical lineage, SQL, ABAP, profiling)
Both share a single term selector via st.session_state so switching tabs
preserves the selected term.
"""
import streamlit as st
import pandas as pd
import duckdb
import csv
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from db import query, get_connection
from dbt_sync import sync_seed, sync_models, sync_tests
from _csv_safeguard import assert_csv_safe, assert_csv_safe_row_count
from _term_status_utils import filter_active_terms
from _s2t_tab_helpers import (
    STATUS_COLORS,
    classify_dbt_error,
    extract_trailing_digits,
    get_s2t_action,
    has_piece8_s2t_rows,
    is_s2t_eligible,
    render_pipeline_strip,
    render_status_badge,
    render_details_panel,
)
from _compiled_state import (
    load_deployed_keys,
    is_deployed,
    read_compiled_sql,
)

st.title("📖 Business Glossary")
st.caption("Business term definitions · Source-to-target mapping · Data profiling · Approval workflow")
st.divider()

DB_PATH = Path(__file__).resolve().parent.parent.parent / "cpe_analytics.duckdb"
SEED_DIR = Path(__file__).resolve().parent.parent.parent / "dbt" / "seeds"

# OBT view → dashboard page label mapping
OBT_TO_DASHBOARD = {
    'obt_procurement_overview': '📦 Procurement Overview',
    'obt_vendor_scorecard':     '🏢 Vendor Scorecard',
    'obt_cpe_lifecycle':        '📱 CPE Lifecycle',
    'obt_inventory_health':     '📊 Inventory Health',
    'obt_goods_movements':      '🔄 Goods Movements',
}

# Mart / dim / knowledge model → which OBT views consume it
MART_TO_OBT = {
    'fact_purchase_orders':           ['obt_procurement_overview', 'obt_vendor_scorecard'],
    'fact_goods_movements':           ['obt_goods_movements'],
    'fact_equipment_lifecycle':       ['obt_cpe_lifecycle'],
    'fact_inventory':                 ['obt_inventory_health'],
    'fact_invoices':                  ['obt_procurement_overview'],
    'dim_vendor':                     ['obt_procurement_overview', 'obt_vendor_scorecard'],
    'dim_material':                   ['obt_procurement_overview', 'obt_cpe_lifecycle',
                                       'obt_inventory_health', 'obt_goods_movements'],
    'dim_equipment':                  ['obt_cpe_lifecycle'],
    'dim_plant':                      ['obt_inventory_health', 'obt_goods_movements'],
    'knowledge_vendor_performance':   ['obt_vendor_scorecard'],
    'knowledge_cpe_lifecycle_metrics': ['obt_cpe_lifecycle'],
}

# SAP table code → business-friendly name (used in the Term Detail business lineage)
SAP_TABLE_TO_FRIENDLY = {
    'EKKO': 'Purchase Orders',
    'EKPO': 'Purchase Order Items',
    'EKET': 'Purchase Order Schedule Lines',
    'EKBE': 'Purchase Order History',
    'EBAN': 'Purchase Requisitions',
    'EBKN': 'PR Account Assignment',
    'MKPF': 'Goods Receipt Headers',
    'MSEG': 'Goods Receipt Items',
    'MARA': 'Materials',
    'MAKT': 'Material Descriptions',
    'MARD': 'Material Stock',
    'MBEW': 'Material Valuation',
    'LFA1': 'Vendors',
    'LFB1': 'Vendor Accounting',
    'EQUI': 'Equipment Master',
    'EQBS': 'Equipment Status',
    'T001W': 'Plants',
    'RBKP': 'Invoice Headers',
    'RSEG': 'Invoice Items',
    'SERI': 'Serial Numbers',
    'OBJK': 'Equipment Objects',
}

# SAP movement type code → plain description (for filter humanization)
MOVEMENT_TYPE_PLAIN = {
    '101': 'actual goods receipts',
    '102': 'goods receipt reversals',
    '122': 'returns to vendor',
    '161': 'customer returns',
    '201': 'issues to cost center',
    '261': 'issues to production',
    '311': 'stock transfers',
    '411': 'inter-company transfers',
    '501': 'receipts without PO',
}


def friendly_source_name(sap_table: str) -> str:
    code = str(sap_table or '').upper().strip()
    return SAP_TABLE_TO_FRIENDLY.get(code, code.title() if code else '')


def humanize_description(text) -> str:
    """Translate a join/filter description from technical SAP jargon to business English.

    Handles the patterns we see in the seeded s2t_mapping rows:
      - TABLE1.FIELD1 = TABLE2.FIELD2  →  "Goods Receipt Items linked to Purchase Orders"
      - BWART = '101'                   →  "actual goods receipts"
      - BSTYP = 'F'                     →  "standard purchase orders"
      - VGABE = '1'                     →  "goods receipt events"
      - hub_vendor / hub_material       →  "vendor data" / "material data"
      - bare GR / PO acronyms           →  "goods receipt" / "purchase order"
      - bare SAP table codes            →  friendly name
    """
    import re
    if not text or not str(text).strip():
        return ''
    t = str(text).strip()

    # CODE='VALUE' (plain explanation) → keep only the plain explanation
    # (handles both bare CODE and TABLE.FIELD forms)
    t = re.sub(
        r"(?:[A-Z][A-Z0-9_]*\.)?[A-Z][A-Z0-9_]*\s*=\s*['\"][^'\"]*['\"]\s*\(([^)]+)\)",
        lambda m: m.group(1),
        t,
    )

    # hub_X → "X data"
    t = re.sub(
        r'\bhub_([a-z_]+)\b',
        lambda m: m.group(1).replace('_', ' ') + ' data',
        t,
    )

    # Primary join pattern: TABLE1.FIELD = TABLE2.FIELD
    def _join_replace(m):
        ln = friendly_source_name(m.group(1))
        rn = friendly_source_name(m.group(2))
        return f"{ln} linked to {rn}"
    t = re.sub(
        r'\b([A-Z][A-Z0-9_]{2,})\.[A-Z0-9_]+\s*=\s*([A-Z][A-Z0-9_]{2,})\.[A-Z0-9_]+',
        _join_replace,
        t,
    )
    # Secondary AND-join pattern
    t = re.sub(
        r'\s+AND\s+([A-Z][A-Z0-9_]{2,})\.[A-Z0-9_]+\s*=\s*([A-Z][A-Z0-9_]{2,})\.[A-Z0-9_]+',
        lambda m: f", {friendly_source_name(m.group(1))} linked to {friendly_source_name(m.group(2))}",
        t,
    )

    # BWART = '101' (or similar) → plain phrase
    t = re.sub(
        r"BWART\s*=\s*['\"]?(\d+)['\"]?",
        lambda m: MOVEMENT_TYPE_PLAIN.get(m.group(1), f"movement type {m.group(1)}"),
        t,
        flags=re.IGNORECASE,
    )
    # BWART IN ('101','102')
    def _bwart_in(m):
        codes = re.findall(r"(\d+)", m.group(0))
        names = [MOVEMENT_TYPE_PLAIN.get(c, f"movement type {c}") for c in codes]
        return " or ".join(names)
    t = re.sub(r"BWART\s+IN\s*\([^\)]*\)", _bwart_in, t, flags=re.IGNORECASE)
    # "Movement type NNN" phrasing
    t = re.sub(
        r"Movement type (\d+)",
        lambda m: MOVEMENT_TYPE_PLAIN.get(m.group(1), f"movement type {m.group(1)}"),
        t,
    )

    # BSTYP='F'
    t = re.sub(r"BSTYP\s*=\s*['\"]?F['\"]?", "standard purchase orders", t, flags=re.IGNORECASE)

    # VGABE='1'
    t = re.sub(r"VGABE\s*=\s*['\"]?1['\"]?", "goods receipt events", t, flags=re.IGNORECASE)

    # Bare acronyms — must run after joins so we don't clobber TABLE.FIELD
    t = re.sub(r'\bGR\b', 'goods receipt', t)
    t = re.sub(r'\bPO\b', 'purchase order', t)
    t = re.sub(r'\bPR\b', 'purchase requisition', t)

    # Strip noise parentheses around column codes
    t = re.sub(r"\s*\((?:BSART|BSTYP|BWART|VGABE|LABST|MENGE|BEDAT|EINDT|BUDAT)\)", '', t)

    # Any remaining bare SAP table codes → friendly name
    def _bare_table(m):
        return friendly_source_name(m.group(0))
    t = re.sub(
        r'\b(EKKO|EKPO|EKET|EKBE|EBAN|EBKN|MKPF|MSEG|MARA|MAKT|MARD|MBEW|LFA1|LFB1|EQUI|EQBS|T001W|RBKP|RSEG|SERI|OBJK)\b',
        _bare_table,
        t,
    )

    # Collapse any leftover `Friendly Name.word` into `Friendly Name word`
    for friendly in SAP_TABLE_TO_FRIENDLY.values():
        t = t.replace(f"{friendly}.", f"{friendly} ")

    # Drop a leading "Filter " prefix — uninformative once the rest is humanized
    t = re.sub(r'^\s*Filter\s+', '', t, flags=re.IGNORECASE)

    # Cleanup whitespace and leading punctuation
    t = re.sub(r'\s+', ' ', t).strip(' ,;')
    if t:
        t = t[0].upper() + t[1:]
    return t


# --- Seed data ---
glossary = query("SELECT * FROM main_seeds.business_glossary ORDER BY id")
s2t = query("SELECT * FROM main_seeds.s2t_mapping ORDER BY id")
sap_dict = query("SELECT * FROM main_seeds.sap_data_dictionary ORDER BY table_name, field_name")
dv_design = query("SELECT * FROM main_seeds.data_vault_design ORDER BY id")
abap_meta = query("SELECT * FROM main_seeds.abap_logic_catalog")
contracts = query("SELECT * FROM main_seeds.data_contracts ORDER BY id")


# ============================================================
# HELPERS
# ============================================================

def compute_lineage(term_s2t):
    """Resolve source → staging → vault → mart → obt → dashboard for a term."""
    source_tables_list = term_s2t['source_table'].dropna().unique().tolist()
    staging_models = sorted({f"stg_sap__{t.lower()}" for t in source_tables_list})

    vault_entities = []
    if not dv_design.empty:
        src_upper = [t.upper() for t in source_tables_list]
        for _, dv in dv_design.iterrows():
            _raw = dv.get('source_tables', '')
            src_tables = (str(_raw).strip() if pd.notna(_raw) else '').upper()
            if any(t in src_tables for t in src_upper):
                vault_entities.append(
                    f"{str(dv['entity_type'])[0].upper()}: {dv['entity_name']}"
                )

    target_models_list = term_s2t['target_model'].dropna().unique().tolist()

    obt_views, dashboard_pages = [], []
    seen_obts, seen_dash = set(), set()
    for tm in target_models_list:
        for obt in MART_TO_OBT.get(tm, []):
            if obt not in seen_obts:
                obt_views.append(obt)
                seen_obts.add(obt)
            dash = OBT_TO_DASHBOARD.get(obt)
            if dash and dash not in seen_dash:
                dashboard_pages.append(dash)
                seen_dash.add(dash)

    return {
        "source_tables": source_tables_list,
        "staging_models": staging_models,
        "vault_entities": vault_entities,
        "target_models": target_models_list,
        "obt_views": obt_views,
        "dashboard_pages": dashboard_pages,
    }


def render_flow_diagram(lin):
    """Render the 6-node SAP → Dashboard flow diagram.

    Sized tight so all six boxes fit the viewport at 100% zoom without
    horizontal scrolling — target ~120px per box, 9px body font, 8px
    layer label, minimal arrow gaps. The total width budget is ~820px.
    """
    def _col(items, max_items=6):
        if not items:
            return "<span style='color:#6b7280;font-size:9px;font-style:italic'>—</span>"
        shown = items[:max_items]
        extra = len(items) - len(shown)
        html_items = "".join(
            f"<div style='font-family:SF Mono,Consolas,monospace;font-size:9px;"
            f"color:#e0e0e0;padding:1px 0;white-space:nowrap;overflow:hidden;"
            f"text-overflow:ellipsis'>{it}</div>"
            for it in shown
        )
        if extra > 0:
            html_items += (
                f"<div style='font-size:8px;color:#6b7280;padding-top:2px'>+{extra} more</div>"
            )
        return html_items

    nodes = [
        ("SAP Source", "#6b7280", "#1f2937", "#374151", lin["source_tables"]),
        ("Staging",    "#3b82f6", "#172554", "#1e3a5f", lin["staging_models"]),
        ("Vault",      "#8b5cf6", "#2e1065", "#3b0764", lin["vault_entities"]),
        ("Mart",       "#4ade80", "#14532d", "#166534", lin["target_models"]),
        ("OBT",        "#f59e0b", "#422006", "#92400e", lin["obt_views"]),
        ("Dashboard",  "#f87171", "#7f1d1d", "#991b1b", lin["dashboard_pages"]),
    ]

    parts = [
        "<div style=\"display:flex;align-items:stretch;gap:2px;"
        "padding:6px 2px;font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI',sans-serif;\">"
    ]
    for i, (label, accent, bg, border, items) in enumerate(nodes):
        parts.append(
            f"<div style='min-width:120px;flex:1 1 0;background:{bg};"
            f"border:1px solid {border};border-radius:6px;padding:6px 8px;"
            f"border-top:3px solid {accent};overflow:hidden;'>"
            f"<div style='font-size:8px;font-weight:700;color:{accent};"
            f"text-transform:uppercase;letter-spacing:0.8px;margin-bottom:4px;'>"
            f"{label}</div>"
            f"{_col(items)}</div>"
        )
        if i < len(nodes) - 1:
            parts.append(
                "<div style='display:flex;align-items:center;color:#4b5563;"
                "font-size:12px;flex-shrink:0;padding:0 1px'>&rarr;</div>"
            )
    parts.append("</div>")

    max_items = max(
        (len(x) for x in (lin["source_tables"], lin["staging_models"], lin["vault_entities"],
                          lin["target_models"], lin["obt_views"], lin["dashboard_pages"])),
        default=1,
    )
    # ~14px per item row + ~30px chrome (label + padding + border)
    height = max(110, 44 + min(max_items, 6) * 14 + (16 if max_items > 6 else 0))
    st.components.v1.html("".join(parts), height=height, scrolling=False)


def render_business_lineage(term_s2t, dashboard_pages, term_row=None):
    """Render the Term Detail business lineage as a vertical flow: source boxes → rules → reports.

    One box per SAP source table, titled with the business-friendly name and showing the
    exact columns (by source_description) used for this term. Below the source row, the
    join and filter descriptions are humanized and shown as a plain-language meta line.
    Middle section shows the numbered business rules. Bottom shows the destination
    dashboard pages as report boxes. No SQL, no staging/vault/mart/OBT references.
    """
    def _escape(s):
        return (
            str(s)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
        )

    # Group source rows by SAP table
    source_groups = []
    for tbl in term_s2t['source_table'].dropna().unique():
        rows = term_s2t[term_s2t['source_table'] == tbl]
        items = []
        seen = set()
        for _, r in rows.iterrows():
            _raw_desc = r.get('source_description', '')
            desc = str(_raw_desc).strip() if pd.notna(_raw_desc) else ''
            if not desc:
                _raw_field = r.get('source_field', '')
                desc = str(_raw_field).strip() if pd.notna(_raw_field) else ''
            if desc and desc not in seen:
                items.append(desc)
                seen.add(desc)
        source_groups.append({
            "name": friendly_source_name(tbl),
            "sap_code": str(tbl).upper(),
            "items": items or ["—"],
        })

    # Prefer business-audience descriptions (Rule 7); fall back to technical
    plain_steps = []
    for _, _r in term_s2t.iterrows():
        _raw_biz = _r.get('transformation_logic_plain_business', '')
        biz = str(_raw_biz).strip() if pd.notna(_raw_biz) else ''
        _raw_tech = _r.get('transformation_logic_plain', '')
        tech = str(_raw_tech).strip() if pd.notna(_raw_tech) else ''
        desc = biz if (biz and biz != 'nan') else tech
        if desc and desc != 'nan':
            plain_steps.append(desc)

    # Prefer term-scoped business descriptions from glossary (Rule 7)
    _biz_join = ""
    _biz_filter = ""
    if term_row is not None:
        _raw_bj = term_row.get('business_join_description', '')
        _biz_join = str(_raw_bj).strip() if pd.notna(_raw_bj) else ''
        _raw_bf = term_row.get('business_filter_description', '')
        _biz_filter = str(_raw_bf).strip() if pd.notna(_raw_bf) else ''
        if _biz_join == 'nan':
            _biz_join = ""
        if _biz_filter == 'nan':
            _biz_filter = ""

    if _biz_join:
        joins_plain = [_biz_join]
    else:
        joins_plain = [
            humanize_description(j)
            for j in term_s2t['join_description'].dropna().unique().tolist()
            if str(j).strip()
        ]
        joins_plain = [j for j in joins_plain if j]

    if _biz_filter:
        filters_plain = [f.strip() for f in _biz_filter.split(';') if f.strip()]
    else:
        filters_plain = [
            humanize_description(f)
            for f in term_s2t['filter_description'].dropna().unique().tolist()
            if str(f).strip()
        ]
        filters_plain = [f for f in filters_plain if f]

    # Build HTML
    source_boxes = "".join(
        f"""
        <div class="bl-box bl-box-source">
            <div class="bl-label bl-label-source">Source</div>
            <div class="bl-title">{_escape(g['name'])}</div>
            <div class="bl-subtitle">Raw: {_escape(g['sap_code'])}</div>
            <ul class="bl-items">
                {''.join(f'<li>{_escape(it)}</li>' for it in g['items'])}
            </ul>
        </div>
        """
        for g in source_groups
    )

    meta_parts = []
    if joins_plain:
        meta_parts.append(
            f"<div><strong>How data is combined:</strong> {_escape('; '.join(joins_plain))}.</div>"
        )
    if filters_plain:
        meta_parts.append(
            f"<div><strong>What's included:</strong> {_escape('; '.join(filters_plain))}.</div>"
        )
    meta_html = (
        f"<div class='bl-meta'>{''.join(meta_parts)}</div>" if meta_parts else ""
    )

    steps_html = "".join(f"<li>{_escape(s)}</li>" for s in plain_steps) or "<li>—</li>"

    report_boxes = "".join(
        f"""
        <div class="bl-box bl-box-report">
            <div class="bl-label bl-label-report">Report</div>
            <div class="bl-title">{_escape(d)}</div>
        </div>
        """
        for d in dashboard_pages
    ) or """
        <div class="bl-box bl-box-report">
            <div class="bl-label bl-label-report">Report</div>
            <div class="bl-title">Not yet displayed on any dashboard</div>
        </div>
        """

    html = f"""
    <style>
        .bl-container {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            padding: 4px 2px 12px 2px;
        }}
        .bl-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            justify-content: center;
            align-items: stretch;
        }}
        .bl-box {{
            background: #131a2e;
            border: 1px solid #1e2a45;
            border-radius: 10px;
            padding: 12px 14px;
            min-width: 200px;
            max-width: 260px;
            flex: 1 1 200px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.25);
        }}
        .bl-box-source    {{ border-top: 3px solid #6b7280; }}
        .bl-box-transform {{ border-top: 3px solid #3b82f6; }}
        .bl-box-report    {{ border-top: 3px solid #4ade80; }}

        .bl-label {{
            font-size: 10px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 1.2px;
            margin-bottom: 4px;
        }}
        .bl-label-source    {{ color: #9ca3af; }}
        .bl-label-transform {{ color: #60a5fa; }}
        .bl-label-report    {{ color: #4ade80; }}

        .bl-title {{
            font-size: 14px;
            font-weight: 600;
            color: #e0e0e0;
            margin-bottom: 2px;
            line-height: 1.25;
        }}
        .bl-subtitle {{
            font-size: 10px;
            color: #6b7280;
            font-family: 'SF Mono', Consolas, monospace;
            margin-bottom: 10px;
            letter-spacing: 0.4px;
        }}
        .bl-items {{
            list-style: none;
            padding: 0;
            margin: 0;
        }}
        .bl-items li {{
            color: #cbd5e1;
            font-size: 12px;
            padding: 3px 0 3px 14px;
            position: relative;
            line-height: 1.35;
        }}
        .bl-items li::before {{
            content: '•';
            position: absolute;
            left: 2px;
            color: #60a5fa;
            font-size: 14px;
            line-height: 12px;
        }}

        .bl-meta {{
            color: #8892a4;
            font-size: 11px;
            font-style: italic;
            background: rgba(19, 26, 46, 0.6);
            border: 1px solid #1e2a45;
            border-radius: 6px;
            padding: 8px 12px;
            margin: 10px 0 0 0;
        }}
        .bl-meta strong {{
            color: #cbd5e1;
            font-style: normal;
            font-weight: 600;
        }}
        .bl-meta div + div {{ margin-top: 4px; }}

        .bl-arrow {{
            text-align: center;
            color: #4b5563;
            font-size: 26px;
            line-height: 1;
            padding: 10px 0 4px 0;
        }}

        .bl-transform-box {{
            background: #131a2e;
            border: 1px solid #1e2a45;
            border-radius: 10px;
            padding: 14px 18px;
            border-top: 3px solid #3b82f6;
            margin: 0 auto;
            box-shadow: 0 2px 8px rgba(0,0,0,0.25);
            max-width: 880px;
        }}
        .bl-step-list {{
            list-style: none;
            padding: 0;
            margin: 8px 0 0 0;
            counter-reset: step;
        }}
        .bl-step-list li {{
            color: #e0e0e0;
            font-size: 13px;
            padding: 6px 0 6px 34px;
            position: relative;
            counter-increment: step;
            line-height: 1.5;
        }}
        .bl-step-list li::before {{
            content: counter(step);
            position: absolute;
            left: 0; top: 4px;
            background: #1e3a5f;
            color: #60a5fa;
            width: 24px; height: 24px;
            border-radius: 50%;
            text-align: center;
            font-size: 11px;
            font-weight: 700;
            line-height: 24px;
        }}
    </style>

    <div class="bl-container">
        <div class="bl-row">
            {source_boxes}
        </div>
        {meta_html}

        <div class="bl-arrow">↓</div>

        <div class="bl-transform-box">
            <div class="bl-label bl-label-transform">Business Rules</div>
            <div class="bl-title">🔧 Transformation</div>
            <ol class="bl-step-list">
                {steps_html}
            </ol>
        </div>

        <div class="bl-arrow">↓</div>

        <div class="bl-row">
            {report_boxes}
        </div>
    </div>
    """

    # Height estimate — keep everything visible with minimal trailing whitespace.
    # Values tuned against a Playwright DOM measurement, slightly conservative so
    # long terms don't get cut off but short terms don't leave a dead iframe gap.
    boxes_per_row = 3
    num_sources = max(1, len(source_groups))
    source_rows = (num_sources + boxes_per_row - 1) // boxes_per_row
    max_items_in_box = max((len(g["items"]) for g in source_groups), default=1)
    source_box_h = 78 + max_items_in_box * 20  # header + items
    source_section_h = source_rows * (source_box_h + 10)

    meta_h = (14 + len(meta_parts) * 22) if meta_parts else 0
    transform_h = 78 + len(plain_steps) * 30

    num_reports = max(1, len(dashboard_pages) or 1)
    report_rows = (num_reports + boxes_per_row - 1) // boxes_per_row
    # Each report row needs: 24px box padding + 14px label + ~20px title
    # line + 3px accent border + 12px inter-row gap — round up to 110 so
    # two-word titles like "Procurement Overview" / "Vendor Scorecard"
    # are never clipped at the bottom.
    report_h = report_rows * 110

    arrow_h = 32
    # Bottom safety margin — prevents the last row of report boxes from
    # butting up against the iframe edge (the .bl-container already has
    # 12px of bottom padding but iframes tend to round down).
    padding_h = 56
    total_h = source_section_h + meta_h + arrow_h + transform_h + arrow_h + report_h + padding_h
    st.components.v1.html(html, height=total_h, scrolling=False)


def render_source_quality(conn, term_s2t):
    """Source data quality table — row count + avg completeness per source."""
    rows = []
    for tbl in term_s2t['source_table'].dropna().unique():
        stg = f"main_staging.stg_sap__{tbl.lower()}"
        fields = term_s2t[term_s2t['source_table'] == tbl]['source_field'].dropna().tolist()
        try:
            rc = conn.execute(f"SELECT COUNT(*) FROM {stg}").fetchone()[0]
        except Exception:
            continue
        completeness_vals = []
        for f in fields:
            try:
                c = conn.execute(
                    f"SELECT ROUND(100.0 * COUNT({f}) / NULLIF(COUNT(*),0), 1) FROM {stg}"
                ).fetchone()[0]
                if c is not None:
                    completeness_vals.append(float(c))
            except Exception:
                pass
        if completeness_vals:
            avg_comp = round(sum(completeness_vals) / len(completeness_vals), 1)
            status = "✅ Healthy" if avg_comp >= 95 else ("⚠️ Review" if avg_comp >= 80 else "❌ Low")
            comp_str = f"{avg_comp}%"
        else:
            status, comp_str = "—", "n/a"
        rows.append({
            "Data source": tbl,
            "Rows": f"{rc:,}",
            "Field completeness": comp_str,
            "Status": status,
        })
    if rows:
        st.markdown("**Source data quality**")
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        return True
    return False


def render_target_availability(conn, term_s2t):
    """Target dataset row counts — 'Available in reports' table."""
    target_rows = []
    for tm in term_s2t['target_model'].dropna().unique():
        for schema in ['main_marts', 'main_obt', 'main_knowledge']:
            try:
                rc = conn.execute(f"SELECT COUNT(*) FROM {schema}.{tm}").fetchone()[0]
                target_rows.append({"Report dataset": tm, "Rows": f"{rc:,}"})
                break
            except Exception:
                continue
    if target_rows:
        st.markdown("**Available in reports**")
        st.dataframe(pd.DataFrame(target_rows), use_container_width=True, hide_index=True)
        return True
    return False


def execute_dq_test(test_sql: str, conn) -> tuple:
    """Execute a dbt singular test and return (passed, violation_count, error).

    Resolves ``{{ ref('x') }}`` jinja tokens by probing known schemas.
    Returns ``(True, 0, None)`` on pass, ``(False, N, None)`` on fail,
    or ``(False, -1, error_str)`` on execution error.
    """
    import re as _re

    exec_sql = test_sql
    for ref_name in _re.findall(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}", exec_sql):
        resolved = None
        for schema in ('main_marts', 'main_obt', 'main_knowledge', 'main_vault', 'main_staging'):
            try:
                conn.execute(f'SELECT 1 FROM "{schema}"."{ref_name}" LIMIT 0').fetchone()
                resolved = f'"{schema}"."{ref_name}"'
                break
            except Exception:
                continue
        if resolved:
            exec_sql = _re.sub(
                r"\{\{\s*ref\(\s*['\"]" + _re.escape(ref_name) + r"['\"]\s*\)\s*\}\}",
                resolved,
                exec_sql,
            )

    try:
        count = conn.execute(
            f"SELECT COUNT(*) FROM ({exec_sql}) _dq_violations"
        ).fetchone()[0]
        count = int(count)
        return (count == 0, count, None)
    except Exception as e:
        return (False, -1, str(e))


def render_dq_status(conn, term_s2t):
    """Run every dbt singular test in dbt/tests/ whose SQL references this term's
    target model, and show live pass/fail status.

    Business rule description is extracted from the file header in this order:
      1. `-- DQ Rule: <rule>` (written by the Data Quality tab generator)
      2. The first comment line that isn't a known provenance header
    """
    import re as _re

    if term_s2t.empty:
        st.caption("Data quality checks will be available after S2T mapping is defined.")
        return

    target_models = term_s2t['target_model'].dropna().unique().tolist()
    if not target_models:
        st.caption("Data quality checks will be available after S2T mapping is defined.")
        return

    tests_dir = SEED_DIR.parent / "tests"
    if not tests_dir.exists():
        st.info("No `dbt/tests/` directory found.")
        return

    test_files = sorted(tests_dir.glob("*.sql"))
    relevant = []
    for tf in test_files:
        try:
            content = tf.read_text(encoding='utf-8')
        except Exception:
            continue
        if any(tm in content for tm in target_models):
            relevant.append((tf.stem, content))

    if not relevant:
        st.info(
            "No data quality rules defined for this term yet. "
            "Open the **🛡️ Data Quality** tab to create one."
        )
        return

    # Provenance comment headers we should skip when extracting the rule text
    _skip_prefixes = (
        '-- business term:', '-- target:', '-- severity:',
        '-- generated by', '-- dq rule:',
    )

    results = []
    for test_name, test_sql in relevant:
        rule_desc = ''
        for raw_line in test_sql.splitlines():
            line = raw_line.strip()
            if not line.startswith('--'):
                continue
            low = line.lower()
            if low.startswith('-- dq rule:'):
                rule_desc = line.split(':', 1)[1].strip()
                break
            if low.startswith(_skip_prefixes):
                continue
            # First generic comment — use as fallback description
            rule_desc = line.lstrip('- ').strip()
            # Keep going in case a later '-- DQ Rule:' overrides it
        if not rule_desc:
            rule_desc = test_name.replace('_', ' ')

        passed, violation_count, error = execute_dq_test(test_sql, conn)
        if error:
            results.append({
                'test_name': test_name,
                'rule': rule_desc,
                'violations': None,
                'status_label': f"⚠️ Error: {error[:60]}",
                'passed': False,
                'error': error,
            })
        else:
            status_label = (
                "✅ PASS" if passed else f"❌ FAIL ({violation_count:,} violations)"
            )
            results.append({
                'test_name': test_name,
                'rule': rule_desc,
                'violations': violation_count,
                'status_label': status_label,
                'passed': passed,
                'error': None,
            })

    # Header summary
    total = len(results)
    passing = sum(1 for r in results if r['passed'])
    failing = sum(1 for r in results if not r['passed'] and r['error'] is None)
    errored = sum(1 for r in results if r['error'] is not None)

    if failing == 0 and errored == 0:
        st.success(f"✅ All {total} data quality rules pass.")
    elif failing > 0:
        st.error(f"❌ {failing} of {total} rules have violations.")
    else:
        st.warning(f"⚠️ {errored} of {total} rules could not be evaluated.")

    for r in results:
        color = "#4ade80" if r['passed'] else ("#f87171" if r['error'] is None else "#fbbf24")
        st.markdown(
            f"<span style='color:{color};font-weight:600'>{r['status_label']}</span> "
            f"— {r['rule']} "
            f"<span style='color:#6b7280;font-family:monospace;font-size:11px'>({r['test_name']})</span>",
            unsafe_allow_html=True,
        )


def render_full_profile(conn, term_s2t):
    """Column-level profiling for every source and target column referenced by this term."""
    st.markdown("#### Source Tables Profile")
    for tbl in term_s2t['source_table'].dropna().unique():
        stg_table = f"main_staging.stg_sap__{tbl.lower()}"
        try:
            row_count = conn.execute(f"SELECT COUNT(*) FROM {stg_table}").fetchone()[0]
            cols = conn.execute(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema = 'main_staging' AND table_name = 'stg_sap__{tbl.lower()}'"
            ).fetchdf()
            st.markdown(f"**{stg_table}**: {row_count:,} rows, {len(cols)} columns")

            fields_for_table = term_s2t[term_s2t['source_table'] == tbl]['source_field'].dropna().tolist()
            profile_rows = []
            for field in fields_for_table:
                try:
                    stats = conn.execute(f"""
                        SELECT
                            '{field}' AS field,
                            COUNT(*) AS total_rows,
                            COUNT({field}) AS non_null,
                            ROUND(100.0 * COUNT({field}) / COUNT(*), 1) AS completeness_pct,
                            COUNT(DISTINCT {field}) AS distinct_values,
                            CAST(MIN(CAST({field} AS VARCHAR)) AS VARCHAR) AS min_value,
                            CAST(MAX(CAST({field} AS VARCHAR)) AS VARCHAR) AS max_value
                        FROM {stg_table}
                    """).fetchdf()
                    profile_rows.append(stats.iloc[0].to_dict())
                except Exception:
                    profile_rows.append({
                        "field": field, "total_rows": "?", "non_null": "?",
                        "completeness_pct": "?", "distinct_values": "?",
                        "min_value": "?", "max_value": "?",
                    })
            if profile_rows:
                st.dataframe(pd.DataFrame(profile_rows), use_container_width=True, hide_index=True)
        except Exception as e:
            st.caption(f"Could not profile {stg_table}: {e}")

    st.markdown("#### Target Table Profile")
    for tm in term_s2t['target_model'].dropna().unique():
        for schema in ['main_marts', 'main_obt', 'main_knowledge']:
            target_table = f"{schema}.{tm}"
            try:
                row_count = conn.execute(f"SELECT COUNT(*) FROM {target_table}").fetchone()[0]
            except Exception:
                continue

            target_cols = term_s2t[term_s2t['target_model'] == tm]['target_column'].dropna().unique().tolist()
            profile_rows = []
            for col in target_cols:
                try:
                    stats = conn.execute(f"""
                        SELECT
                            '{col}' AS column_name,
                            COUNT(*) AS total_rows,
                            COUNT({col}) AS non_null,
                            ROUND(100.0 - 100.0 * COUNT({col}) / COUNT(*), 1) AS null_pct,
                            COUNT(DISTINCT {col}) AS distinct_values,
                            ROUND(AVG(CAST({col} AS DOUBLE)), 2) AS avg_value,
                            MIN(CAST({col} AS DOUBLE)) AS min_value,
                            MAX(CAST({col} AS DOUBLE)) AS max_value,
                            ROUND(PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY CAST({col} AS DOUBLE)), 2) AS p25,
                            ROUND(PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY CAST({col} AS DOUBLE)), 2) AS p50,
                            ROUND(PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY CAST({col} AS DOUBLE)), 2) AS p75
                        FROM {target_table}
                    """).fetchdf()
                    profile_rows.append(stats.iloc[0].to_dict())
                except Exception:
                    try:
                        stats = conn.execute(f"""
                            SELECT
                                '{col}' AS column_name,
                                COUNT(*) AS total_rows,
                                COUNT({col}) AS non_null,
                                ROUND(100.0 - 100.0 * COUNT({col}) / COUNT(*), 1) AS null_pct,
                                COUNT(DISTINCT {col}) AS distinct_values,
                                NULL AS avg_value,
                                CAST(MIN(CAST({col} AS VARCHAR)) AS VARCHAR) AS min_value,
                                CAST(MAX(CAST({col} AS VARCHAR)) AS VARCHAR) AS max_value,
                                NULL AS p25, NULL AS p50, NULL AS p75
                            FROM {target_table}
                        """).fetchdf()
                        profile_rows.append(stats.iloc[0].to_dict())
                    except Exception as e:
                        profile_rows.append({"column_name": col, "error": str(e)[:80]})

            if profile_rows:
                st.markdown(f"**{target_table}**: {row_count:,} rows")
                profile_df = pd.DataFrame(profile_rows)
                if 'error' in profile_df.columns and profile_df['error'].isna().all():
                    profile_df = profile_df.drop(columns=['error'])
                st.dataframe(profile_df, use_container_width=True, hide_index=True)
            break


def render_contract_compliance(term_s2t):
    """Data contract compliance table + details expander."""
    st.subheader("📜 Data Contract Compliance")
    if term_s2t.empty or contracts.empty:
        st.info("Data contracts will appear after S2T mapping is defined.")
        return

    source_tables_for_term = term_s2t['source_table'].dropna().unique().tolist()
    relevant_contracts = contracts[
        contracts['source_table'].isin([t.upper() for t in source_tables_for_term])
    ]
    if relevant_contracts.empty:
        st.info("No data contracts defined for the source tables used by this term.")
        return

    contract_conn = get_connection()  # in-memory Parquet-backed; do not close
    try:
        all_compliant = True
        contract_rows = []
        for _, contract in relevant_contracts.iterrows():
            stg_table = f"main_staging.stg_sap__{contract['source_table'].lower()}"
            fields_for_table = term_s2t[
                term_s2t['source_table'].str.upper() == contract['source_table'].upper()
            ]['source_field'].dropna().tolist()

            completeness_results = []
            for field in fields_for_table:
                try:
                    r = contract_conn.execute(
                        f"SELECT ROUND(100.0 * COUNT({field}) / COUNT(*), 1) FROM {stg_table}"
                    ).fetchone()[0]
                    completeness_results.append(r)
                except Exception:
                    completeness_results.append(None)

            valid = [r for r in completeness_results if r is not None]
            actual_completeness = min(valid) if valid else None
            threshold = float(contract['completeness_threshold_pct'])

            if actual_completeness is not None:
                compliant = actual_completeness >= threshold
                if not compliant:
                    all_compliant = False
                status = "✅ PASS" if compliant else "❌ BREACH"
                actual_str = f"{actual_completeness}%"
            else:
                status = "⚠️ UNKNOWN"
                actual_str = "?"

            contract_rows.append({
                "Source Table": contract['source_table'],
                "Producer": contract['producer_team'],
                "Frequency": contract['extraction_frequency'],
                "SLA": contract['extraction_sla_utc'],
                "Freshness": f"≤ {contract['freshness_max_hours']}h",
                "Completeness Target": f"{contract['completeness_threshold_pct']}%",
                "Completeness Actual": actual_str,
                "Status": status,
                "Schema Notice": f"{contract['schema_change_notice_days']}d",
            })
    finally:
        pass  # cached in-memory connection — do not close

    st.dataframe(pd.DataFrame(contract_rows), use_container_width=True, hide_index=True)
    if all_compliant:
        st.success("✅ All data contracts compliant for this business term.")
    else:
        st.error("❌ One or more data contracts breached — review source data quality.")

    with st.expander("📋 Contract details"):
        for _, contract in relevant_contracts.iterrows():
            st.markdown(
                f"**{contract['source_table']}** — {contract['notes']}\n\n"
                f"Producer: {contract['producer_team']} ({contract['producer_contact']})\n\n"
                f"Extraction: {contract['extraction_frequency']} by {contract['extraction_sla_utc']} UTC\n\n"
                f"Schema change notice: {contract['schema_change_notice_days']} days"
            )
            st.divider()


def render_approval_form(term, term_id):
    st.subheader("✅ Approval")
    st.markdown(f"**Current status:** {term['status'].upper()}")

    with st.form(key=f"approval_form_{term_id}"):
        action = st.radio(
            "Action",
            ["No change", "Approve", "Deny / Request changes"],
            index=0, horizontal=True,
        )
        comment = st.text_area("Comment (required for Deny)", placeholder="Enter reason...")
        approver_name = st.text_input("Your name", placeholder="e.g., Head of Supply Chain")
        submitted = st.form_submit_button("Submit Decision")

        if submitted:
            if action == "No change":
                st.info("No changes made.")
            elif action == "Approve":
                if not approver_name:
                    st.error("Please enter your name to approve.")
                else:
                    csv_path = SEED_DIR / "business_glossary.csv"
                    rows = []
                    with open(csv_path, 'r', encoding='utf-8', newline='') as f:
                        reader = csv.DictReader(f)
                        fieldnames = reader.fieldnames
                        for row in reader:
                            if row['id'] == term_id:
                                row['status'] = 'approved'
                                row['approved_by'] = approver_name
                            rows.append(row)
                    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(rows)
                    sync_seed(
                        "business_glossary",
                        success_msg=(
                            f"✅ Term '{term['display_name']}' approved by {approver_name} "
                            "and synced to the database."
                        ),
                    )
            elif action == "Deny / Request changes":
                if not comment:
                    st.error("Please provide a reason for denial.")
                elif not approver_name:
                    st.error("Please enter your name.")
                else:
                    csv_path = SEED_DIR / "business_glossary.csv"
                    rows = []
                    with open(csv_path, 'r', encoding='utf-8', newline='') as f:
                        reader = csv.DictReader(f)
                        fieldnames = reader.fieldnames
                        for row in reader:
                            if row['id'] == term_id:
                                row['status'] = 'denied'
                                existing_notes = row.get('notes', '') or ''
                                row['notes'] = f"{existing_notes} | DENIED by {approver_name}: {comment}".strip(' |')
                            rows.append(row)
                    with open(csv_path, 'w', encoding='utf-8', newline='') as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(rows)
                    st.warning(
                        f"❌ Term '{term['display_name']}' denied by {approver_name}. Reason: {comment}"
                    )
                    sync_seed(
                        "business_glossary",
                        success_msg="✅ Denial recorded and synced to the database.",
                    )


def _load_term_analysis_findings(term_id: str):
    """Return analysis_findings rows for this term, or an empty DataFrame.

    Cross-term inheritance was removed (2026-04-18).
    Each term owns its own findings; a re-created term with the
    same term_name starts from zero and must run fresh Guided Analysis.
    Archive is final — audit trail stays with the archived term, new
    term gets a clean slate. See RULE 42 revision #5.

    Load-all-then-filter (instead of WHERE term_id = ... in SQL) keeps
    the DuckDB result cache warm across selector changes and avoids
    string-concatenating user ids into SQL.
    """
    try:
        df = query("SELECT * FROM main_seeds.analysis_findings")
        if df.empty or 'business_term_id' not in df.columns:
            return pd.DataFrame()
        return df[df['business_term_id'].astype(str) == str(term_id)]
    except Exception:
        return pd.DataFrame()


def render_ask_claude(term, term_id):
    """Claude-backed Create S2T entry point. Stage D.2 dual-path:
      - Legacy path: analysis_findings rows exist (the original
        guided-analysis workflow).
      - New pipeline path: status in ('ready_for_s2t', 'approved').

    Status-specific messaging for ineligible terms now lives in
    tab_spec's action panel (S4 resolution); render_ask_claude here
    only handles the archived hard-stop error inline and silent-returns
    for other ineligible states. Caller gates this call on
    action['show_create_button'] so ineligible terms never reach here
    in practice — the silent return is defensive.
    """
    term_findings_df = _load_term_analysis_findings(term_id)
    has_legacy_findings = not term_findings_df.empty

    eligible, reason = is_s2t_eligible(term, has_legacy_findings)
    if not eligible:
        if reason == "archived_hard_stop":
            st.error("⚠️ Cannot create S2T for archived term.")
        # Other ineligible reasons: caller's action panel already rendered
        # the appropriate status-specific messaging. Silent return.
        return

    status = str(term.get("status", "")).strip().lower()
    is_new_pipeline = status in ("ready_for_s2t", "approved")

    # Legacy-path context banner + missing_data gate. New-pipeline terms
    # skip this entire block — they reach Create S2T through Stage A/B/C
    # evidence instead of analysis_findings rows.
    if has_legacy_findings and not is_new_pipeline:
        st.success(
            f"✅ Data analysis completed ({len(term_findings_df)} findings). "
            "Claude can now create a reliable S2T mapping and the dbt models to implement it."
        )
        with st.expander(f"📊 Analysis findings ({len(term_findings_df)} findings)", expanded=False):
            for _, f_row in term_findings_df.iterrows():
                _raw_s = f_row.get('result_summary', '')
                summary = (str(_raw_s).strip() if pd.notna(_raw_s) else '')[:300]
                st.markdown(f"- **{f_row.get('finding_type', '?')}:** {summary}")

        # --- Missing-data blocker: legacy-path only ---
        # The session_state flag was written by the legacy Data Analysis UI
        # that Stage D.1 deleted, so it is now inert. Kept for defense in
        # depth in case anything else ever sets it.
        is_blocked = bool(st.session_state.get(f"s2t_blocked_{term_id}", False))
        session_block_reason = st.session_state.get(f"s2t_blocked_reason_{term_id}", []) or []

        persisted_missing = term_findings_df[
            term_findings_df.get('finding_type', '') == 'missing_data'
        ] if 'finding_type' in term_findings_df.columns else pd.DataFrame()

        if is_blocked or not persisted_missing.empty:
            st.error("🚫 Cannot create S2T — missing source data identified during analysis.")
            st.markdown("**Missing data:**")
            if session_block_reason:
                for m in session_block_reason:
                    tbl = m.get('table', '?')
                    cols = m.get('missing_columns') or []
                    col_list = ", ".join(f"`{c}`" for c in cols) if cols else "_(unspecified)_"
                    why = m.get('why_needed', '')
                    st.markdown(f"- **{tbl}** — missing {col_list}" + (f" — {why}" if why else ""))
            elif not persisted_missing.empty:
                for _, row in persisted_missing.iterrows():
                    _raw_t = row.get('tables_explored', '')
                    _t_str = str(_raw_t).strip() if pd.notna(_raw_t) else ''
                    tbl = _t_str if _t_str else '?'
                    _raw_c = row.get('columns_explored', '')
                    cols = str(_raw_c).strip() if pd.notna(_raw_c) else ''
                    col_list = ", ".join(f"`{c}`" for c in cols.split(';') if c) or "_(unspecified)_"
                    _raw_rs = row.get('result_summary', '')
                    summary = (str(_raw_rs).strip() if pd.notna(_raw_rs) else '')[:250]
                    st.markdown(f"- **{tbl}** — missing {col_list}")
                    if summary:
                        st.caption(f"    {summary}")
            st.markdown(
                "**Action needed:** load the missing tables/columns into the raw_sap schema, "
                "re-run the **🔬 Data Analysis** page for this term, and return here once the "
                "gaps are resolved."
            )
            return

    st.divider()
    st.markdown("### 🚀 Create S2T Mapping")
    st.markdown(
        """Claude will use the analysis findings to create:
- S2T mapping rows (source tables, fields, transformation logic, target model/column)
- the dbt model SQL for any **new** layers that are needed
- a layer-aware implementation plan — if the data already exists in vault or marts,
  we only build what's missing on top of it (never rebuild existing layers)"""
    )

    # Freshness gate: S2T build is a write-path action — blocked on red.
    from freshness import render_freshness_banner as _ffresh_banner_s2t, is_write_blocked as _is_blocked_s2t
    _ffresh_banner_s2t("s2t")
    _s2t_blocked = _is_blocked_s2t()

    if st.button(
        "🤖 Create S2T with Claude",
        key=f"create_s2t_{term_id}",
        type="primary",
        disabled=_s2t_blocked,
        help=(
            "Domain facts are stale. Re-run ingestion and refresh before building S2T."
            if _s2t_blocked else None
        ),
    ):
        # RULE 41: reject archived-term clicks before any LLM work. Streamlit
        # session state can surface a stale button press against a term that
        # was archived after the page first rendered. No point spending tokens.
        if str(term.get('status', '')).strip().lower() == 'archived':
            st.error(
                f"Cannot create S2T for archived term '{term.get('term_name', '')}'. "
                f"This term was archived on {term.get('archived_at_utc', '') or 'unknown date'}. "
                "Select an active term or create a fresh one via the New Term form."
            )
            return
        with st.spinner("Claude is creating the S2T specification and dbt implementation plan..."):
            from claude_api import create_s2t_with_implementation

            # P7.2 migration — all 9 inline context loads (actual_schema, abap_catalog,
            # existing_glossary, existing_s2t, analysis_findings, existing_models,
            # column_lineage, domain_context, archived_context) are now assembled
            # inside create_s2t_with_implementation via assemble_context(purpose='create_s2t').
            # The caller only needs to pass the term scalars + term_id.
            result = create_s2t_with_implementation(
                term_name=term['display_name'],
                term_definition=term['definition'],
                term_unit=term['unit'],
                term_grain=term['grain'],
                term_id=term_id,
            )

        if result and "error" not in result:
            st.session_state[f"s2t_create_{term_id}"] = result
        elif result and result.get("_refusal_kind") == "bar_needs_data_extension":
            # C6 Finding D — Stage D's iteration loop already concluded
            # the term is unanswerable from current scope. Render the
            # BAR's verdict + sourcing_recommendations + reachability
            # violations via the existing Closure 3/4 renderer.
            st.error(
                f"⛔ Stage E refused to generate SQL — BAR "
                f"`{result.get('_bar_id')}` says "
                f"`{result.get('_bar_status')}` "
                f"(reason: `{result.get('_bar_convergence_reason') or 'unspecified'}`). "
                "The system cannot produce a reliable specification until the "
                "data gap is closed (see analysis result below)."
            )
            from _bar_section import render_bar_section
            # Forward _bar_id so renderer reads the exact BAR the
            # dispatcher cited (live DuckDB), bypassing stale parquet.
            render_bar_section(
                term_id, query, bar_id=result.get("_bar_id"),
            )
        elif result and result.get("_refusal_kind") == "bridge_coverage_violation":
            # C6 — post-generation gate refused LLM-emitted SQL because
            # filter values are empirically unreachable through the
            # chosen join path.
            st.error(
                "⛔ Stage E generated SQL but the post-generation "
                "bridge-coverage gate refused it: the LLM filtered on "
                "values empirically unreachable through the chosen joins."
            )
            violations = result.get("_bridge_violations") or []
            if violations:
                st.markdown("**Violations:**")
                for _v in violations:
                    st.markdown(f"- {_v}")
            st.info(
                "Re-run Stage D's iteration loop to surface "
                "sourcing_recommendations for this term, or revise the "
                "term's scope to include reachable values."
            )
        elif result:
            st.error(f"Claude API error: {result.get('error', 'Unknown error')}")
            if 'raw_response' in result:
                with st.expander("Raw response"):
                    st.code(result['raw_response'])

    creation = st.session_state.get(f"s2t_create_{term_id}")
    if creation and "error" not in creation:
        st.success("✅ Claude has generated the S2T specification and implementation plan.")

        plan = creation.get("implementation_plan", {}) or {}
        start_layer = str(plan.get("start_layer", "raw") or "raw")
        layers_needed = plan.get("layers_needed", []) or []
        plan_note = str(plan.get("explanation", "") or "")

        col_a, col_b = st.columns([1, 2])
        with col_a:
            st.markdown(f"**Starting layer:** `{start_layer}`")
        with col_b:
            if layers_needed:
                st.markdown(f"**Layers to build:** {' → '.join(layers_needed)}")
        if plan_note:
            st.caption(plan_note)

        st.markdown("### S2T Mapping")
        sources = creation.get("s2t_mapping", []) or []
        if sources:
            st.dataframe(pd.DataFrame(sources), use_container_width=True, hide_index=True)

        st.markdown("### Transformation Logic")
        transform_plain = str(creation.get("transformation_plain", "") or "")
        if transform_plain:
            st.markdown(transform_plain)

        transform_sql = str(creation.get("transformation_sql", "") or "")
        if transform_sql:
            st.markdown("### SQL Implementation")
            st.code(transform_sql, language="sql")

        dbt_models = creation.get("dbt_models", []) or []
        if dbt_models:
            st.markdown(f"### dbt Models to Create ({len(dbt_models)})")
            for model in dbt_models:
                filename = str(model.get('filename', 'model.sql') or 'model.sql')
                layer = str(model.get('layer', 'marts') or 'marts')
                desc = str(model.get('description', '') or '')
                with st.expander(f"📄 {filename}  ({layer} layer)"):
                    if desc:
                        st.markdown(f"**Purpose:** {desc}")
                    st.code(model.get('sql', '') or '', language="sql")

        warnings = creation.get("warnings", []) or []
        if warnings:
            st.markdown("### ⚠️ Warnings")
            for w in warnings:
                st.warning(w)

        confidence = str(creation.get("confidence", "") or "").lower()
        if confidence:
            conf_color = {"high": "#4ade80", "medium": "#fbbf24", "low": "#f87171"}.get(confidence, "#6b7280")
            st.markdown(
                f"**Claude's confidence:** <span style='color:{conf_color}'>{confidence.upper()}</span>",
                unsafe_allow_html=True,
            )

        st.divider()

        if st.button(
            "🚀 Deploy models",
            key=f"deploy_models_{term_id}",
            type="primary",
        ):
            # RULE 41: reject archived-term clicks before any file-write or
            # dbt invocation. Streamlit button-widget state keyed by
            # `deploy_models_{term_id}` can survive a hard refresh if the
            # term was archived after the page first rendered; without this
            # guard the Deploy handler would write phantom s2t rows against
            # a target_model whose .sql has already been moved to archive/.
            if str(term.get('status', '')).strip().lower() == 'archived':
                st.error(
                    f"Cannot deploy archived term '{term.get('term_name', '')}'. "
                    f"This term was archived on {term.get('archived_at_utc', '') or 'unknown date'}. "
                    "Select an active term or create a fresh one via the New Term form."
                )
                return
            # Full inline pipeline with atomic rollback.
            # Does NOT touch business_glossary status (Rule 22).
            PROJECT_ROOT = SEED_DIR.parent.parent  # = <project>/
            dbt_root = SEED_DIR.parent  # = <project>/dbt
            s2t_csv = SEED_DIR / "s2t_mapping.csv"
            log_path = PROJECT_ROOT / "deploy_debug.log"
            written_files = []
            s2t_rows_added = 0
            written_s2t_ids = []  # RULE 39: rollback by ID, not by position
            new_model_names = []
            failed_step = None
            fail_error = ""

            def _log(msg):
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(f"[{datetime.now().isoformat()}] {msg}\n")

            def _rollback():
                # RULE 39: delete Step a's appended rows by tracked S0xx IDs,
                # not by `iloc[:-s2t_rows_added]`. Rule 14 inside end_of_task
                # can insert placeholder rows AFTER Step a's appends; a
                # position-based rollback removes the wrong rows and leaves
                # the CSV in an inconsistent state. ID match is robust to any
                # mid-pipeline reshuffling that doesn't touch Step a's IDs.
                #
                # RULE 39 extension #2: also drop Rule 14
                # placeholder rows that piggy-backed on the new model files
                # being deleted here. Without this, rollback leaves orphan
                # rows pointing at .sql files it just removed, creating CSV
                # vs models drift that the next scan has to clean up. Scope
                # the placeholder cleanup to business_term_id == term_id so
                # we don't touch rows the Deploy never wrote.
                _log("ROLLBACK started")
                deleted_model_names = {
                    Path(fp).stem for fp in written_files
                    if str(fp).endswith('.sql')
                }
                if written_s2t_ids or deleted_model_names:
                    try:
                        df = pd.read_csv(s2t_csv, keep_default_na=False, dtype=str)
                        before = len(df)
                        step_a_mask = df['id'].astype(str).isin(written_s2t_ids)
                        orphan_mask = (
                            df['business_term_id'].astype(str).eq(str(term_id))
                            & df['target_model'].isin(deleted_model_names)
                            & ~step_a_mask  # don't double-count Step a rows
                        )
                        df_new = df[~(step_a_mask | orphan_mask)]
                        after = len(df_new)
                        removed_step_a = int(step_a_mask.sum())
                        removed_orphans = int(orphan_mask.sum())
                        assert_csv_safe(s2t_csv, df_new)
                        df_new.to_csv(s2t_csv, index=False, lineterminator='\n')
                        _log(
                            f"  Rolled back {before - after} rows: "
                            f"{removed_step_a} Step-a (tracked {written_s2t_ids}) + "
                            f"{removed_orphans} Rule-14 orphans for deleted models "
                            f"{sorted(deleted_model_names)} scoped to term_id={term_id}"
                        )
                    except Exception as rb_err:
                        _log(f"  S2T rollback failed: {rb_err}")
                for fp in written_files:
                    try:
                        fp.unlink()
                        _log(f"  Deleted {fp}")
                    except Exception as rb_err:
                        _log(f"  File delete failed {fp}: {rb_err}")
                _log("ROLLBACK complete")

            # Resolve dbt executable from the same venv as the running Python
            _dbt_exe = str(Path(sys.executable).parent / "dbt.EXE")

            def _run_subprocess(cmd, cwd, step_name, timeout=300):
                """Run a subprocess with timeout. Returns (stdout, stderr) or raises.

                Default 300s matches dbt seed/run/test which are bounded.
                Caller passes `timeout=900` for Step f end_of_task — the deploy
                auto-retry can trigger Rule 14 placeholder insertions which in turn
                force sync_s2t_plain LLM cache misses for each new row; the
                aggregate can exceed 5 min on a cold cache.
                """
                _log(f"  subprocess START: {' '.join(str(c) for c in cmd)} cwd={cwd}")
                result = subprocess.run(
                    cmd, check=True, capture_output=True, text=True,
                    timeout=timeout, cwd=str(cwd),
                )
                _log(f"  subprocess END: rc={result.returncode} stdout={len(result.stdout)}chars")
                return result.stdout, result.stderr

            _log(f"=== DEPLOY START term={term_id} ({term['display_name']}) ===")

            with st.status("Deploying models...", expanded=True) as status:

                # --- Step a: Write S2T mapping rows ---
                st.write("-> Writing S2T mapping rows...")
                _log("Step a: Write S2T rows")
                try:
                    existing_s2t = pd.read_csv(s2t_csv)
                    # known_issue #83: use regex-based trailing-digit
                    # extraction instead of unanchored replace('S', '').
                    # Handles mixed ID schemes (SNNN legacy, S2T-NNNN
                    # modern, BG028-NN term-prefixed). New rows use the
                    # modern S2T-NNNN scheme for consistency with
                    # scope_derivation stubs; max-based numbering
                    # prevents collisions across schemes.
                    max_num = int(
                        existing_s2t['id'].apply(extract_trailing_digits).max() or 0
                    )
                    with open(s2t_csv, 'a', encoding='utf-8', newline='') as f:
                        writer = csv.DictWriter(
                            f, fieldnames=existing_s2t.columns.tolist(), lineterminator='\n',
                        )
                        for i, src in enumerate(sources):
                            row = {
                                'id': f"S2T-{max_num + i + 1:04d}",
                                'business_term_id': term_id,
                                'business_term_name': term['term_name'],
                                'source_table': src.get('source_table', ''),
                                'source_field': src.get('source_field', ''),
                                'source_description': src.get('source_description', ''),
                                'transformation_logic_plain': src.get('transformation_logic_plain', ''),
                                'transformation_logic_sql': '',
                                'join_description': src.get('join_description', ''),
                                'filter_description': src.get('filter_description', ''),
                                'target_model': src.get('target_model', ''),
                                'target_column': src.get('target_column', ''),
                                'notes': f"Generated by Claude create_s2t_with_implementation (confidence: {confidence or 'unknown'})",
                            }
                            writer.writerow({c: row.get(c, '') for c in existing_s2t.columns})
                            s2t_rows_added += 1
                            # RULE 39: tag the exact ID this append wrote so
                            # rollback can target it even if Rule 14 or other
                            # downstream processing reshuffles the CSV later.
                            written_s2t_ids.append(row['id'])
                    # SAFEGUARD: post-append row count must pass seed bounds (catches
                    # any writer that somehow truncated the file mid-append).
                    assert_csv_safe_row_count(s2t_csv, len(existing_s2t) + s2t_rows_added)
                    st.write(f"OK S2T mapping: {s2t_rows_added} rows written")
                    _log(f"  S2T rows written: {s2t_rows_added}")
                except Exception as e:
                    failed_step = "S2T mapping"
                    fail_error = str(e)
                    _log(f"  FAILED: {e}")

                # --- Step b: Create dbt model files ---
                if not failed_step and dbt_models:
                    st.write("-> Creating dbt model files...")
                    _log("Step b: Create dbt model files")
                    try:
                        for model in dbt_models:
                            layer = str(model.get('layer', 'marts') or 'marts')
                            filename = str(model.get('filename', 'new_model.sql') or 'new_model.sql')
                            safe_name = re.sub(r'[^a-zA-Z0-9_.]', '_', filename)
                            if not safe_name.endswith('.sql'):
                                safe_name += '.sql'
                            sql_content = str(model.get('sql', '') or '').strip() + "\n"
                            target_dir = dbt_root / "models" / layer
                            target_dir.mkdir(parents=True, exist_ok=True)
                            filepath = target_dir / safe_name
                            with open(filepath, 'w', encoding='utf-8', newline='\n') as f:
                                f.write(sql_content)
                            written_files.append(filepath)
                            new_model_names.append(safe_name.replace('.sql', ''))
                        model_list_str = ", ".join(new_model_names)
                        st.write(f"OK {len(written_files)} models created: {model_list_str}")
                        _log(f"  Models created: {model_list_str}")
                    except Exception as e:
                        failed_step = "dbt model creation"
                        fail_error = str(e)
                        _log(f"  FAILED: {e}")

                # --- Step c: dbt seed ---
                if not failed_step:
                    st.write("-> Running dbt seed...")
                    _log("Step c: dbt seed")
                    try:
                        stdout, stderr = _run_subprocess(
                            [_dbt_exe, "seed", "--select", "s2t_mapping"],
                            cwd=dbt_root,
                            step_name="dbt seed",
                        )
                        seed_summary = "done"
                        for line in stdout.splitlines():
                            if "OK loaded seed" in line or "PASS" in line:
                                seed_summary = line.strip()
                                break
                        st.write(f"OK dbt seed: {seed_summary}")
                    except subprocess.CalledProcessError as e:
                        failed_step = "dbt seed"
                        fail_error = e.stderr or e.stdout or str(e)
                        _log(f"  FAILED: {fail_error}")
                    except subprocess.TimeoutExpired:
                        failed_step = "dbt seed"
                        fail_error = "Timed out after 5 minutes"
                        _log(f"  TIMEOUT")

                # --- Step d: dbt run (auto-retry with LLM error feedback) ---
                # When LLM-generated SQL references a non-existent column (classic
                # hallucination — e.g. `material_description` when the candidate is
                # `model_description`), dbt run fails with a parseable Binder Error.
                # Feed the error + DuckDB's candidate bindings + information_schema
                # dump back to the LLM for a corrected SQL. Max 3 attempts total.
                # After that, rollback with a pointer to Guided Analysis.
                dbt_run_attempt = 0
                dbt_run_succeeded = False
                dbt_run_max_attempts = 3
                last_run_error = ""

                if not failed_step and new_model_names:
                    select_arg = " ".join(new_model_names)

                    # known_issue #84: classifier widened from Binder-only
                    # to cover Catalog/Parser/Type/IO/OOM classes + catch-
                    # all. Lives in _s2t_tab_helpers.classify_dbt_error
                    # for testability; returns dict(should_retry, hint,
                    # failed_col, candidates, failed_model).

                    def _schema_dump_for_sql(sql_text: str) -> str:
                        """Find ref('x') models in sql_text and dump each one's
                        actual column list from information_schema. Scans all
                        schemas where a ref() target could live."""
                        refs = re.findall(r"ref\(\s*['\"]([^'\"]+)['\"]\s*\)", sql_text)
                        if not refs:
                            return "(no ref() calls detected in SQL)"
                        try:
                            conn = get_connection()
                        except Exception as e:
                            return f"(schema lookup unavailable: {e})"
                        candidate_schemas = (
                            'main_staging', 'main_marts', 'main_obt',
                            'main_knowledge', 'main_vault', 'main_seeds',
                        )
                        parts = []
                        for model_name in sorted(set(refs)):
                            found = False
                            for sch in candidate_schemas:
                                try:
                                    df = conn.execute(
                                        "SELECT column_name, data_type "
                                        "FROM information_schema.columns "
                                        f"WHERE table_schema = '{sch}' "
                                        f"  AND table_name = '{model_name}' "
                                        "ORDER BY ordinal_position"
                                    ).fetchdf()
                                except Exception:
                                    continue
                                if not df.empty:
                                    parts.append(
                                        f"\n{sch}.{model_name}:\n" +
                                        "\n".join(
                                            f"  {r['column_name']} ({r['data_type']})"
                                            for _, r in df.iterrows()
                                        )
                                    )
                                    found = True
                                    break
                            if not found:
                                parts.append(f"\n{model_name}: (not found in any schema)")
                        return "\n".join(parts) if parts else "(no schemas resolved)"

                    while dbt_run_attempt < dbt_run_max_attempts and not dbt_run_succeeded:
                        dbt_run_attempt += 1
                        label = (
                            f"dbt run (attempt {dbt_run_attempt}/{dbt_run_max_attempts})"
                            if dbt_run_attempt > 1 else "dbt run"
                        )
                        st.write(f"-> Running {label} --select {select_arg}...")
                        _log(f"Step d: {label} --select {select_arg}")
                        try:
                            stdout, stderr = _run_subprocess(
                                [_dbt_exe, "run", "--select", select_arg],
                                cwd=dbt_root,
                                step_name=label,
                            )
                            run_summary = "done"
                            for line in stdout.splitlines():
                                if "Completed successfully" in line or "Done." in line:
                                    run_summary = line.strip()
                                    break
                            st.write(f"OK dbt run: {run_summary} (attempt {dbt_run_attempt})")
                            _log(f"  dbt run SUCCESS on attempt {dbt_run_attempt}/{dbt_run_max_attempts}")
                            dbt_run_succeeded = True
                            break
                        except subprocess.CalledProcessError as e:
                            last_run_error = e.stderr or e.stdout or str(e)
                            _log(f"  attempt {dbt_run_attempt} FAILED: {last_run_error[:300]}")
                        except subprocess.TimeoutExpired:
                            last_run_error = "Timed out after 5 minutes"
                            _log(f"  attempt {dbt_run_attempt} TIMEOUT")
                            break  # timeouts don't benefit from retry

                        # known_issue #84: classify the error class and
                        # fetch the class-specific repair hint. Non-timeout
                        # errors now get retried (was: Binder-only).
                        _classification = classify_dbt_error(last_run_error)
                        failed_col = _classification["failed_col"]
                        candidates = _classification["candidates"]
                        failed_model = _classification["failed_model"]
                        _error_hint = _classification["hint"]
                        if not _classification["should_retry"]:
                            _log(f"  error class opts out of retry (timeout) — skipping retry")
                            break

                        if dbt_run_attempt >= dbt_run_max_attempts:
                            _log(f"  max retries reached ({dbt_run_max_attempts}) — giving up")
                            break

                        # Identify the .sql file for the failed model. Prefer the
                        # failed_model parsed from the error; fall back to the
                        # first written file if parsing didn't yield a model name.
                        target_sql_path = None
                        if failed_model:
                            for fp in written_files:
                                if fp.stem == failed_model:
                                    target_sql_path = fp
                                    break
                        if target_sql_path is None and written_files:
                            target_sql_path = written_files[0]
                        if target_sql_path is None:
                            _log(f"  no target .sql path resolved — skipping retry")
                            break

                        try:
                            current_sql = target_sql_path.read_text(encoding='utf-8')
                        except Exception as e:
                            _log(f"  could not read {target_sql_path}: {e}")
                            break

                        schema_dump = _schema_dump_for_sql(current_sql)
                        st.write(
                            f"-> Deferred: asking Claude to fix `{failed_col}` in "
                            f"`{target_sql_path.name}` (candidates: "
                            f"{', '.join(candidates[:5]) if candidates else 'none'})"
                        )
                        _log(
                            f"  LLM repair: col={failed_col!r} "
                            f"candidates={candidates[:5]} "
                            f"target={target_sql_path.name}"
                        )
                        try:
                            from claude_api import repair_dbt_model_sql
                            repair = repair_dbt_model_sql(
                                model_filename=target_sql_path.name,
                                current_sql=current_sql,
                                dbt_error_text=last_run_error[:3000],
                                schema_dump=schema_dump,
                                error_hint=_error_hint,  # #84
                            )
                        except Exception as e:
                            _log(f"  LLM repair call raised: {e}")
                            break
                        if "error" in repair or not repair.get("sql"):
                            _log(f"  LLM repair returned no usable sql: {repair.get('error', 'empty')}")
                            break
                        fixed_sql = repair["sql"].strip()
                        if not fixed_sql.endswith("\n"):
                            fixed_sql += "\n"
                        try:
                            with open(target_sql_path, 'w', encoding='utf-8', newline='\n') as f:
                                f.write(fixed_sql)
                            _log(f"  LLM repair: overwrote {target_sql_path.name} ({len(fixed_sql)} chars)")
                        except Exception as e:
                            _log(f"  failed writing repaired SQL: {e}")
                            break
                        # next loop iteration re-runs dbt run

                    if not dbt_run_succeeded:
                        failed_step = "dbt run"
                        fail_error = (
                            last_run_error
                            + f"\n\nDeploy failed after {dbt_run_attempt} "
                            f"attempt(s) (max {dbt_run_max_attempts}). The term may "
                            "need more domain context. Go to Data Analysis → Guided "
                            "Analysis — Business Term, select this term, and run "
                            "analysis to help the LLM understand the source schema "
                            "better. Then retry Deploy."
                        )

                # --- Step d.5: semantic validation gate ---
                # SQL that compiles and runs is not proof it measures what
                # the business term claims to measure. Validate across
                # grain / filter / unit via LLM. Max 3 repair attempts per
                # model. Conservative stance: only block on critical
                # mismatch, warnings are informational only.
                SEMANTIC_MAX_ATTEMPTS = 3
                if not failed_step and new_model_names and dbt_run_succeeded:
                    # Build term_row dict from the Streamlit `term` Series
                    # that is already in the enclosing render_ask_claude
                    # scope. Use plain dict access so the helper sees the
                    # same shape as other API calls in this module.
                    term_row_dict = {
                        "id": term_id,
                        "term_name": term.get("term_name", ""),
                        "display_name": term.get("display_name", ""),
                        "definition": term.get("definition", ""),
                        "unit": term.get("unit", ""),
                        "grain": term.get("grain", ""),
                        "notes": term.get("notes", ""),
                    }
                    for sql_path in written_files:
                        if not str(sql_path).endswith(".sql"):
                            continue
                        model_name = sql_path.stem
                        # Schema is derived from parent dir: dbt/models/<layer>/ → main_<layer>.
                        layer_name = sql_path.parent.name
                        schema_name = f"main_{layer_name}"
                        st.write(
                            f"-> Semantic validation for "
                            f"`{schema_name}.{model_name}`..."
                        )
                        _log(f"Step d.5 semantic: {schema_name}.{model_name}")

                        sem_ok = False
                        broken_by_repair = False
                        last_issues = []
                        for sem_attempt in range(1, SEMANTIC_MAX_ATTEMPTS + 1):
                            # Pull fresh metadata each iteration — repair
                            # may have re-run dbt which changed the view.
                            try:
                                current_sql = sql_path.read_text(encoding="utf-8")
                                count_df = query(
                                    f'SELECT COUNT(*) AS n FROM {schema_name}."{model_name}"'
                                )
                                row_count = int(count_df.iloc[0]["n"])
                                sample_df = query(
                                    f'SELECT * FROM {schema_name}."{model_name}" LIMIT 5'
                                )
                                sample_rows = sample_df.to_dict("records")
                                schema_df = query(
                                    f'DESCRIBE {schema_name}."{model_name}"'
                                )
                                column_types = {
                                    str(r.get("column_name") or r.get("name", "")):
                                        str(r.get("column_type") or r.get("type", ""))
                                    for _, r in schema_df.iterrows()
                                }
                            except Exception as e:
                                # Can't read metadata — don't block Deploy.
                                # The downstream dbt test step will catch
                                # actual data-quality failures.
                                _log(f"  semantic gate: metadata read failed: {e}")
                                sem_ok = True
                                break

                            try:
                                from claude_api import (
                                    validate_model_semantics,
                                    repair_semantic_mismatch,
                                )
                                validation = validate_model_semantics(
                                    term_row=term_row_dict,
                                    model_name=model_name,
                                    model_sql=current_sql,
                                    row_count=row_count,
                                    column_types=column_types,
                                    sample_rows=sample_rows,
                                )
                            except Exception as e:
                                _log(f"  validate_model_semantics raised: {e}")
                                sem_ok = True
                                break
                            if "error" in validation:
                                _log(f"  semantic validator error: {validation['error']}")
                                sem_ok = True
                                break

                            issues = validation.get("issues", []) or []
                            critical = [
                                i for i in issues
                                if (i.get("severity") or "").lower() == "critical"
                            ]
                            summary = validation.get("summary", "") or ""
                            _log(
                                f"  semantic attempt {sem_attempt}/"
                                f"{SEMANTIC_MAX_ATTEMPTS}: match="
                                f"{validation.get('match')} "
                                f"critical={len(critical)} "
                                f"warnings={len(issues) - len(critical)} "
                                f"summary={summary[:160]!r}"
                            )

                            if validation.get("match") or not critical:
                                st.write(
                                    f"OK semantic PASS on attempt "
                                    f"{sem_attempt}/{SEMANTIC_MAX_ATTEMPTS}: {summary}"
                                )
                                sem_ok = True
                                break

                            last_issues = critical
                            if sem_attempt >= SEMANTIC_MAX_ATTEMPTS:
                                _log(
                                    f"  semantic FAILED after "
                                    f"{SEMANTIC_MAX_ATTEMPTS} attempts: "
                                    f"{[i.get('dimension') for i in critical]}"
                                )
                                break

                            # Repair and re-run dbt run before next iteration.
                            dims = [i.get("dimension", "?") for i in critical]
                            st.write(
                                f"-> Semantic repair "
                                f"{sem_attempt}/{SEMANTIC_MAX_ATTEMPTS} "
                                f"({', '.join(dims)})..."
                            )
                            try:
                                repair = repair_semantic_mismatch(
                                    term_row=term_row_dict,
                                    model_sql=current_sql,
                                    issues=critical,
                                )
                            except Exception as e:
                                _log(f"  repair_semantic_mismatch raised: {e}")
                                break
                            if "error" in repair or not repair.get("sql"):
                                _log(
                                    f"  repair returned no usable sql: "
                                    f"{repair.get('error', 'empty')}"
                                )
                                break
                            repaired_sql = repair["sql"].strip()
                            if not repaired_sql.endswith("\n"):
                                repaired_sql += "\n"
                            try:
                                with open(sql_path, "w", encoding="utf-8", newline="\n") as f:
                                    f.write(repaired_sql)
                                _log(
                                    f"  semantic repair: overwrote "
                                    f"{sql_path.name} ({len(repaired_sql)} chars)"
                                )
                            except Exception as e:
                                _log(f"  failed writing semantic-repaired SQL: {e}")
                                break

                            # Re-run dbt for the repaired SQL so the next
                            # validation iteration sees the new view state.
                            try:
                                _run_subprocess(
                                    [_dbt_exe, "run", "--select", model_name],
                                    cwd=dbt_root,
                                    step_name=f"dbt run (semantic repair {sem_attempt})",
                                )
                            except subprocess.CalledProcessError as e:
                                broken_by_repair = True
                                last_run_error = e.stderr or e.stdout or str(e)
                                _log(
                                    f"  semantic repair produced broken SQL: "
                                    f"{last_run_error[:300]}"
                                )
                                break
                            except subprocess.TimeoutExpired:
                                broken_by_repair = True
                                last_run_error = "Timed out re-running dbt after semantic repair"
                                break

                        if broken_by_repair:
                            failed_step = "semantic repair"
                            fail_error = (
                                last_run_error
                                + "\n\nSemantic repair produced broken SQL. "
                                "This usually means the term definition is ambiguous "
                                "or conflicts with available source data. Review term "
                                "definition and retry."
                            )
                            break
                        if not sem_ok:
                            failed_step = "semantic validation"
                            fail_error = (
                                f"Deploy aborted: generated model `{model_name}` "
                                f"semantically mismatched term definition after "
                                f"{SEMANTIC_MAX_ATTEMPTS} repair attempts.\n\n"
                                f"Unresolved critical issues: "
                                f"{[i.get('dimension') + ': ' + (i.get('description') or '')[:120] for i in last_issues]}\n\n"
                                "Run Business Term Analysis to give the LLM "
                                "deeper domain context, then retry Deploy."
                            )
                            break

                # --- Step e: dbt test ---
                if not failed_step and new_model_names:
                    select_arg = " ".join(new_model_names)
                    st.write(f"-> Running dbt test --select {select_arg}...")
                    _log(f"Step e: dbt test --select {select_arg}")
                    try:
                        stdout, stderr = _run_subprocess(
                            [_dbt_exe, "test", "--select", select_arg],
                            cwd=dbt_root,
                            step_name="dbt test",
                        )
                        # Parse test results
                        pass_count = stdout.count("PASS")
                        fail_count = stdout.count("FAIL")
                        test_summary = f"{pass_count} passed"
                        if fail_count > 0:
                            test_summary += f", {fail_count} failed"
                        st.write(f"OK dbt test: {test_summary}")
                    except subprocess.CalledProcessError as e:
                        # dbt test returns non-zero if tests fail — treat as warning, not rollback
                        fail_count = (e.stdout or "").count("FAIL")
                        if fail_count > 0:
                            st.write(f"WARNING dbt test: {fail_count} test(s) failed — review needed")
                            _log(f"  dbt test: {fail_count} failures (non-fatal)")
                        else:
                            failed_step = "dbt test"
                            fail_error = e.stderr or e.stdout or str(e)
                            _log(f"  FAILED: {fail_error}")
                    except subprocess.TimeoutExpired:
                        failed_step = "dbt test"
                        fail_error = "Timed out after 5 minutes"
                        _log(f"  TIMEOUT")

                # --- Step f: end_of_task.py ---
                # Timeout raised to 900s (from the 300s default): when the deploy
                # auto-retry rewrites a model, Rule 14 in sync_s2t_from_dbt inserts
                # new placeholder rows and sync_s2t_plain_from_dbt must then
                # regenerate LLM descriptions for each one (cache miss). That
                # aggregate can push end_of_task past 5 min on a cold cache.
                if not failed_step:
                    st.write("-> Running end_of_task.py (scan + sync + export)...")
                    _log("Step f: end_of_task.py")
                    try:
                        stdout, stderr = _run_subprocess(
                            [sys.executable, str(PROJECT_ROOT / "scripts" / "end_of_task.py")],
                            cwd=PROJECT_ROOT,
                            step_name="end_of_task.py",
                            timeout=900,
                        )
                        st.write("OK Pipeline complete: scan + sync + parquet export")
                    except subprocess.CalledProcessError as e:
                        failed_step = "end_of_task.py"
                        fail_error = e.stderr or e.stdout or str(e)
                        _log(f"  FAILED: {fail_error}")
                    except subprocess.TimeoutExpired:
                        failed_step = "end_of_task.py"
                        fail_error = "Timed out after 15 minutes"
                        _log(f"  TIMEOUT")

                # --- Result ---
                if failed_step:
                    _rollback()
                    status.update(label=f"Deployment failed at: {failed_step}", state="error")
                    _log(f"=== DEPLOY FAILED at {failed_step} ===")
                else:
                    status.update(label="Deployment complete", state="complete")
                    _log("=== DEPLOY SUCCESS ===")

            # Show result outside st.status
            if failed_step:
                st.error(f"Deploy failed at step: **{failed_step}**")
                with st.expander("Error details"):
                    st.code(fail_error[:3000])
                st.warning("All changes have been rolled back.")
                st.caption(f"Debug log: {log_path}")
            else:
                model_list = ", ".join(
                    f"`{fp.relative_to(dbt_root)}`" for fp in written_files
                ) if written_files else "_(no new models needed)_"
                st.success(
                    f"S2T mapping saved ({s2t_rows_added} rows)\n\n"
                    f"{len(written_files)} dbt model(s) created: {model_list}\n\n"
                    f"Pipeline complete: seed + run + test + export\n\n"
                    f"Status unchanged -- data owner must approve **{term['display_name']}** manually"
                )
                st.caption(f"Debug log: {log_path}")
                # Clear Streamlit caches so next query picks up fresh Parquet
                from db import close_connection
                close_connection()


# ============================================================
# SHARED TERM SELECTOR STATE
# ============================================================
# Rule 33: archived terms are preserved as audit record but
# do not appear in the interactive selector. `glossary` (full table)
# stays available to the archive context loader so prior attempts can
# feed the LLM. Stage D.1: centralized via `filter_active_terms`.
_active_glossary = filter_active_terms(glossary)
term_names = _active_glossary['display_name'].tolist()


def _resolve_active_term(selected_name: str):
    """Resolve a display_name selection to a single `_active_glossary` row.

    Lookup-scope must match selector-scope. Archiving
    a term leaves a row with the same `display_name` in the glossary; a
    naive `glossary[display_name==X].iloc[0]` on ORDER-BY-id data picks
    the OLDER (archived) row over the active draft, so every tab ends
    up resolving to the archived term even when the selector only shows
    the active one. Scope the lookup to `_active_glossary` and defensively
    handle zero- and multi-match edge cases.
    """
    matching = _active_glossary[_active_glossary['display_name'] == selected_name]
    if matching.empty:
        st.error(
            f"No active term matches '{selected_name}'. Refresh the page — "
            "the selector may be showing a term that was just archived."
        )
        st.stop()
    if len(matching) > 1:
        # Two actives shouldn't share a display_name (glossary uniqueness),
        # but defensively pick the most-recently created if it ever happens.
        matching = matching.sort_values('created_date', ascending=False)
    return matching.iloc[0]
if term_names:
    st.session_state.setdefault('term_sel_detail', term_names[0])
    st.session_state.setdefault('term_sel_spec', term_names[0])
    st.session_state.setdefault('term_sel_dq', term_names[0])


def _sync_from_detail():
    st.session_state['term_sel_spec'] = st.session_state['term_sel_detail']
    st.session_state['term_sel_dq']   = st.session_state['term_sel_detail']


def _sync_from_spec():
    st.session_state['term_sel_detail'] = st.session_state['term_sel_spec']
    st.session_state['term_sel_dq']     = st.session_state['term_sel_spec']


def _sync_from_dq():
    st.session_state['term_sel_detail'] = st.session_state['term_sel_dq']
    st.session_state['term_sel_spec']   = st.session_state['term_sel_dq']


# ============================================================
# TABS
# ============================================================
tab_overview, tab_detail, tab_spec, tab_dq, tab_new = st.tabs([
    "📋 All Terms",
    "🔍 Term Detail",
    "🔧 S2T Specification",
    "🛡️ Data Quality",
    "➕ New Term",
])


# ============================================================
# TAB 1: OVERVIEW
# ============================================================
@st.cache_data(ttl=60)
def _term_source_map() -> dict:
    """term_id -> source-system tag, inferred from the term's confirmed
    scope tables via the data dictionary's description_source. Interim
    V1 inference — replaced by a first-class source_system column when
    the catalog is consolidated across sources."""
    try:
        df = query("""
            SELECT DISTINCT s.business_term_id AS term_id,
                   CASE WHEN d.description_source = 'olist_kaggle_documentation'
                        THEN 'olist' ELSE 'sap' END AS src
            FROM main_seeds.s2t_mapping s
            JOIN main_seeds.sap_data_dictionary d
              ON LOWER(s.source_table) = LOWER(d.table_name)
        """)
    except Exception:
        return {}
    out: dict = {}
    for _, r in df.iterrows():
        out.setdefault(r["term_id"], set()).add(r["src"])
    return {k: "+".join(sorted(v)) for k, v in out.items()}


with tab_overview:
    st.subheader("Business Terms")

    total = len(glossary)
    approved = len(glossary[glossary['status'] == 'approved'])
    draft = len(glossary[glossary['status'] == 'draft'])
    m1, m2, m3, _ = st.columns([1, 1, 1, 3])
    with m1:
        st.metric("Total Terms", total)
    with m2:
        st.metric("Approved", approved)
    with m3:
        st.metric("Draft", draft)

    status_filter = st.selectbox("Filter by status", ["All", "approved", "draft", "denied"], index=0)
    display_df = glossary if status_filter == "All" else glossary[glossary['status'] == status_filter]

    for _, term in display_df.iterrows():
        status_color = STATUS_COLORS.get(term['status'], STATUS_COLORS['unknown'])
        with st.container():
            col1, col2, col3, col4 = st.columns([3, 1, 1, 1])
            with col1:
                st.markdown(f"**{term['display_name']}** (`{term['term_name']}`)")
                _raw_def = term.get('definition')
                definition = str(_raw_def).strip() if pd.notna(_raw_def) else ''
                st.caption(f"{definition[:150]}...")
            with col2:
                st.markdown(f"<span style='color:{status_color};font-weight:bold'>{term['status'].upper()}</span>", unsafe_allow_html=True)
            with col3:
                st.caption(f"Domain: {term['domain']}")
                _src = _term_source_map().get(term['id'])
                if _src:
                    st.caption(f"Source: {_src}")
            with col4:
                st.caption(f"Grain: {term['grain']}")
            st.divider()


# ============================================================
# TAB 2: TERM DETAIL — business view
# ============================================================
with tab_detail:
    if not term_names:
        st.info("No business terms defined yet.")
    else:
        selected_name = st.selectbox(
            "Select Business Term",
            term_names,
            key="term_sel_detail",
            on_change=_sync_from_detail,
        )

        term = _resolve_active_term(selected_name)
        term_id = term['id']
        term_s2t = s2t[s2t['business_term_id'] == term_id]

        st.subheader(f"📝 {term['display_name']}")

        col_def1, col_def2 = st.columns([2, 1])
        with col_def1:
            st.markdown(f"**Definition:** {term['definition']}")
            st.markdown(f"**Unit:** {term['unit']}  |  **Grain:** {term['grain']}")
            if pd.notna(term.get('notes')) and term['notes']:
                st.info(f"**Notes:** {term['notes']}")
        with col_def2:
            status_color = STATUS_COLORS.get(term['status'], STATUS_COLORS['unknown'])
            st.markdown(
                f"**Status:** <span style='color:{status_color};font-weight:bold;font-size:18px'>{term['status'].upper()}</span>",
                unsafe_allow_html=True,
            )
            st.markdown(f"**Owner:** {term['owner']}")
            st.markdown(f"**Approved by:** {term['approved_by']}")
            st.markdown(f"**Domain:** {term['domain']}")
            st.markdown(f"**Created:** {term['created_date']}")

        related = term.get('related_terms', '')
        if pd.notna(related) and related:
            st.markdown(f"**Related terms:** {', '.join(str(related).split(';'))}")

        st.divider()

        # --- BUSINESS LINEAGE (plain language, no table names, no SQL, no layer names) ---
        st.subheader("📘 Business Lineage")

        if term_s2t.empty:
            # Empty-state: the message is driven purely by term_s2t.empty, not
            # by status. Draft-with-S2T renders the full lineage below so the
            # business owner can review live values and approve; decoupling
            # the wording from status keeps the message accurate whether the
            # term is draft, approved, or denied.
            st.info("No source-to-target mapping defined yet. A data analyst needs to propose one before lineage can render.")
            st.caption("Switch to the **🔧 S2T Specification** tab to propose a mapping with AI assistance.")
        else:
            lin = compute_lineage(term_s2t)
            render_business_lineage(term_s2t, lin["dashboard_pages"], term_row=term)

            # Show plain-language transformation chain for this term's columns only
            try:
                _cl_df = query(
                    "SELECT model_name, column_name, transformation_chain_plain "
                    "FROM main_seeds.dbt_column_lineage "
                    "WHERE transformation_chain_plain IS NOT NULL "
                    "AND transformation_chain_plain != ''"
                )
                _term_target_cols = set(
                    term_s2t['target_column'].dropna().unique()
                )
                target_models = term_s2t['target_model'].dropna().unique()
                for tm in target_models:
                    tm_chains = _cl_df[
                        (_cl_df['model_name'] == tm)
                        & (_cl_df['column_name'].isin(_term_target_cols))
                    ]
                    if not tm_chains.empty:
                        with st.expander("📋 How the data is transformed", expanded=False):
                            for _, cr in tm_chains.iterrows():
                                plain_steps = str(cr['transformation_chain_plain']).split(';')
                                plain_steps = [s.strip() for s in plain_steps if s.strip()]
                                if plain_steps:
                                    col_label = str(cr['column_name']).replace('_', ' ').title()
                                    flow = " -> ".join(plain_steps) + f" -> **{col_label}**"
                                    st.markdown(f"- {flow}")
                        break
            except Exception:
                pass

        st.divider()

        # --- DATA QUALITY SUMMARY (simple, business-level) ---
        #   1. Source data quality         (row count + completeness per source)
        #   2. DQ test results              (live pass/fail per business rule)
        #   3. Available in reports         (row count per target dataset)
        st.subheader("📊 Data Quality Summary")
        if term_s2t.empty:
            st.caption("Profile will appear after S2T mapping is defined.")
        else:
            conn = get_connection()  # in-memory Parquet-backed; do not close
            source_rendered = render_source_quality(conn, term_s2t)

            st.markdown("**DQ test results**")
            render_dq_status(conn, term_s2t)

            target_rendered = render_target_availability(conn, term_s2t)

            if not source_rendered and not target_rendered:
                st.caption("No profiling available — datasets not yet built.")

        st.divider()

        # --- DATA CONTRACT COMPLIANCE ---
        render_contract_compliance(term_s2t)

        st.divider()

        # --- APPROVAL ---
        render_approval_form(term, term_id)

        # --- SAMPLE DATA ---
        target_models_for_sample = term_s2t['target_model'].dropna().unique().tolist()
        for tm in target_models_for_sample:
            if not tm:
                continue
            # Determine schema from model name prefix/layer
            if tm.startswith('obt_'):
                schema = 'main_obt'
            elif tm.startswith('knowledge_'):
                schema = 'main_knowledge'
            elif tm.startswith('stg_sap__'):
                schema = 'main_staging'
            else:
                schema = 'main_marts'
            with st.expander(f"📋 Sample Data — {tm}", expanded=False):
                try:
                    total_count = query(
                        f'SELECT COUNT(*) AS cnt FROM "{schema}"."{tm}"'
                    ).iloc[0]['cnt']
                    sample_df = query(
                        f'SELECT * FROM "{schema}"."{tm}" LIMIT 100'
                    )
                    if total_count <= 100:
                        st.caption(f"Showing all {total_count:,} rows")
                    else:
                        st.caption(f"Showing 100 of {total_count:,} rows")
                    st.dataframe(sample_df, use_container_width=True, hide_index=True)
                except Exception:
                    st.info(
                        "Table not yet deployed — click **Deploy models** "
                        "on the S2T Specification tab to create it."
                    )

        # --- TERM ANALYSIS RESULT (C5 closure 3/4) ---
        # Renders the latest business_term_analysis_results row for the
        # selected term. For status=needs_data_extension, surfaces
        # sourcing_recommendations + reachability violations from the
        # Option B gate so the analyst can decide what to ingest.
        try:
            from _bar_section import render_bar_section
            st.divider()
            render_bar_section(term_id, query)
        except Exception as _exc:  # noqa: BLE001
            st.caption(
                f"Term Analysis Result section unavailable: {_exc}"
            )


# ============================================================
# TAB 3: S2T SPECIFICATION — technical view
# ============================================================
with tab_spec:
    if not term_names:
        st.info("No business terms defined yet.")
    else:
        selected_name = st.selectbox(
            "Select Business Term",
            term_names,
            key="term_sel_spec",
            on_change=_sync_from_spec,
        )

        term = _resolve_active_term(selected_name)
        term_id = term['id']
        term_s2t = s2t[s2t['business_term_id'] == term_id]

        st.subheader(f"🔧 {term['display_name']}")
        st.caption(
            f"Term ID: `{term_id}` · Grain: {term['grain']} · Unit: {term['unit']} · Domain: {term['domain']}"
        )

        # Stage D.2 — pipeline-state awareness. Status badge + strip + action
        # panel sit ABOVE the existing `if term_s2t.empty:` branch so the
        # lineage/SQL/samples/archive panels (lines ~2286-3031 pre-refactor)
        # are preserved verbatim in the `else:` branch below.
        _term_status = str(term.get('status', '')).strip().lower()
        render_status_badge(_term_status, st)
        render_pipeline_strip(_term_status, st)
        st.divider()

        _has_piece8 = has_piece8_s2t_rows(term_s2t)
        _action = get_s2t_action(
            status=_term_status,
            term_id=term_id,
            has_piece8_mapping=_has_piece8,
            glossary_row=dict(term),
        )
        if _action.get('action_text'):
            st.markdown(f"### {_action['action_text']}")
        if _action.get('note'):
            st.caption(_action['note'])
        render_details_panel(
            _action.get('details_key'),
            _action.get('details_data') or {},
            st,
        )
        if _action.get('deep_link_target'):
            st.page_link(
                _action['deep_link_target'],
                label=_action.get('deep_link_label') or '→',
            )
        elif _action.get('deep_link_hint'):
            st.caption(f"→ {_action['deep_link_hint']}")
        st.divider()

        if not _has_piece8:
            # No Create-S2T Deploy output yet — either totally empty
            # s2t_mapping OR Stage A wrote source-only rows with
            # target_model=NULL. In both sub-cases the Create S2T
            # button is the correct affordance (gated on eligibility
            # via action['show_create_button']).
            if _action.get('show_create_button'):
                render_ask_claude(term, term_id)
        else:
            lin = compute_lineage(term_s2t)

            # --- Visual lineage flow ---
            st.markdown("#### 🔗 Lineage Flow")
            render_flow_diagram(lin)

            # --- Source tables ---
            st.markdown("#### 📦 Source Tables")
            source_display = term_s2t[['source_table', 'source_field', 'source_description']].drop_duplicates()
            st.dataframe(source_display, use_container_width=True, hide_index=True)

            source_field_names = term_s2t['source_field'].dropna().unique().tolist()
            relevant_dict = sap_dict[
                (sap_dict['table_name'].isin([t.upper() for t in lin['source_tables']])) &
                (sap_dict['field_name'].isin([f.upper() for f in source_field_names]))
            ] if not sap_dict.empty else pd.DataFrame()
            if not relevant_dict.empty:
                with st.expander("📚 SAP Data Dictionary (field details)"):
                    st.dataframe(
                        relevant_dict[['table_name', 'field_name', 'data_type', 'length',
                                       'description_en', 'business_meaning', 'example_value']],
                        use_container_width=True, hide_index=True,
                    )

            # --- S2T Full SQL per layer (compiled, no jinja) ---
            st.markdown("#### 🔍 S2T Full SQL")
            _compiled_base = Path(__file__).resolve().parent.parent.parent / "dbt" / "target" / "compiled" / "cpe_procurement_analytics" / "models"

            def _trace_refs(start_models, catalog_df, max_depth=4):
                """Trace ref() dependencies backwards, returning models grouped by layer."""
                by_layer = {}
                visited = set()
                frontier = list(start_models)
                for _ in range(max_depth):
                    next_frontier = []
                    for m in frontier:
                        if m in visited:
                            continue
                        visited.add(m)
                        row = catalog_df[catalog_df['model_name'] == m]
                        if row.empty:
                            continue
                        layer = row.iloc[0].get('layer', '')
                        by_layer.setdefault(layer, []).append(m)
                        _raw_refs = row.iloc[0].get('ref_models', '')
                        refs_str = str(_raw_refs).strip() if pd.notna(_raw_refs) else ''
                        if refs_str:
                            for ref in refs_str.split(';'):
                                ref = ref.strip()
                                if ref and ref not in visited:
                                    next_frontier.append(ref)
                    frontier = next_frontier
                    if not frontier:
                        break
                return by_layer

            try:
                _catalog = query(
                    "SELECT model_name, layer, ref_models FROM main_seeds.dbt_model_catalog"
                )
            except Exception:
                _catalog = pd.DataFrame()

            _target_models = term_s2t['target_model'].dropna().unique().tolist()
            if _target_models and not _catalog.empty:
                _by_layer = _trace_refs(_target_models, _catalog)
                _layer_order = [
                    ('staging', '📊 Staging Layer'),
                    ('vault', '🏛️ Data Vault Layer'),
                    ('marts', '📈 Dimensional Model Layer'),
                    ('obt', '📋 OBT'),
                    ('knowledge', '🧠 Knowledge'),
                ]
                # KI-117: DuckDB existence is the primary state check.
                # Filesystem compiled-SQL cache is secondary detail —
                # cache may be stale (only repopulated by selective dbt
                # compile/run); deployed table is unaffected by that
                # staleness.
                _deployed_keys = load_deployed_keys(get_connection())
                for _layer_key, _layer_icon in _layer_order:
                    _models = sorted(set(_by_layer.get(_layer_key, [])))
                    if not _models:
                        continue
                    _label = _layer_icon
                    with st.expander(_label, expanded=False):
                        for _m in _models:
                            _in_db = is_deployed(_m, _layer_key, _deployed_keys)
                            _sql = read_compiled_sql(_m, _compiled_base)
                            if _in_db:
                                st.markdown(f"✅ **{_m}**: deployed (DuckDB)")
                                if _sql:
                                    st.code(_sql, language="sql")
                                else:
                                    st.caption(
                                        f"_Compiled SQL not in dbt cache "
                                        f"(run `dbt compile --select {_m}` "
                                        f"to refresh; deployed table is "
                                        f"unaffected)._"
                                    )
                            elif _sql:
                                st.markdown(
                                    f"⚙️ **{_m}**: compiled, not yet deployed"
                                )
                                st.code(_sql, language="sql")
                            else:
                                st.markdown(
                                    f"❌ **{_m}**: not deployed and no "
                                    f"compiled artifact"
                                )
            else:
                st.caption("No target models mapped yet.")

            # --- UNIFIED Transformation & Lineage (one expander per target column) ---
            st.markdown("#### 🔧 Transformation & Lineage")
            st.caption(
                "One row per target column. Left column shows the business rule "
                "(plain language + joins + filters), right column shows the SQL, "
                "and the bottom strip traces the column from its raw SAP source "
                "through staging, vault, and mart."
            )

            # Load column lineage once — used by every expander below
            try:
                col_lineage_df = query(
                    "SELECT * FROM main_seeds.dbt_column_lineage "
                    "ORDER BY layer, model_name, column_name"
                )
            except Exception:
                col_lineage_df = pd.DataFrame()

            def _find_col(model_name, col_name):
                if col_lineage_df.empty:
                    return None
                m = col_lineage_df[
                    (col_lineage_df['model_name'] == model_name)
                    & (col_lineage_df['column_name'] == col_name)
                ]
                return m.iloc[0] if not m.empty else None

            def _trace(model_name, col_name, max_hops=5):
                chain = []
                seen = set()
                current_model = model_name
                current_col = col_name
                for _ in range(max_hops):
                    key = (current_model, current_col)
                    if key in seen:
                        break
                    seen.add(key)
                    r = _find_col(current_model, current_col)
                    if r is None:
                        break
                    _v_layer = r.get('layer', '')
                    _v_expr = r.get('expression', '')
                    _v_xform = r.get('transformation_type', '')
                    _v_ot = r.get('origin_table', '')
                    _v_oc = r.get('origin_column', '')
                    chain.append({
                        'layer': str(_v_layer).strip() if pd.notna(_v_layer) else '',
                        'model': current_model,
                        'column': current_col,
                        'expression': str(_v_expr).strip() if pd.notna(_v_expr) else '',
                        'transformation': str(_v_xform).strip() if pd.notna(_v_xform) else '',
                        'origin_table': str(_v_ot).strip() if pd.notna(_v_ot) else '',
                        'origin_column': str(_v_oc).strip() if pd.notna(_v_oc) else '',
                    })
                    nxt_model = str(_v_ot).strip() if pd.notna(_v_ot) else ''
                    nxt_col = str(_v_oc).strip() if pd.notna(_v_oc) else ''
                    if not nxt_model or not nxt_col:
                        break
                    current_model, current_col = nxt_model, nxt_col
                return chain

            def _esc(s):
                return (
                    str(s or '')
                    .replace('&', '&amp;').replace('<', '&lt;')
                    .replace('>', '&gt;').replace('"', '&quot;')
                )

            _layer_accent = {
                'source':    '#6b7280',
                'staging':   '#60a5fa',
                'vault':     '#a78bfa',
                'marts':     '#4ade80',
                'obt':       '#fbbf24',
                'knowledge': '#f87171',
                'other':     '#6b7280',
            }

            _col_lineage_css = """
            <style>
                body { margin: 0; background: transparent; }
                .col-lineage-wrap { font-family: -apple-system, 'Segoe UI', sans-serif; }
                .col-lineage-row {
                    display: flex; align-items: stretch; gap: 0;
                    overflow-x: auto; padding: 2px 0;
                }
                .col-branch-label {
                    font-size: 10px; color: #8892a4;
                    font-family: 'SF Mono', Consolas, monospace;
                    margin: 8px 0 2px 4px; letter-spacing: 0.3px;
                }
                .col-branch-label strong { color: #cbd5e1; }
                .col-node {
                    min-width: 170px; max-width: 210px;
                    padding: 9px 11px;
                    background: #131a2e; border: 1px solid #1e2a45;
                    border-radius: 8px; flex-shrink: 0;
                    box-shadow: 0 2px 6px rgba(0,0,0,0.25);
                }
                .col-node-layer {
                    font-size: 9px; font-weight: 700;
                    text-transform: uppercase; letter-spacing: 1px;
                    margin-bottom: 3px;
                }
                .col-node-model {
                    font-size: 10px; color: #8892a4;
                    font-family: 'SF Mono', Consolas, monospace;
                    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                }
                .col-node-column {
                    font-size: 12px; color: #e0e0e0; font-weight: 700;
                    font-family: 'SF Mono', Consolas, monospace;
                    margin-top: 2px;
                    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                }
                .col-node-transform {
                    font-size: 9px; color: #6b7280; margin-top: 4px;
                    font-style: italic; text-transform: uppercase; letter-spacing: 0.3px;
                }
                .col-arrow {
                    color: #3b82f6; font-size: 18px; padding: 0 8px;
                    flex-shrink: 0; display: flex; align-items: center;
                }
                .col-empty {
                    color: #6b7280; font-size: 11px;
                    padding: 6px 12px; font-style: italic;
                }
                .col-converge {
                    margin: 10px 0 8px 0;
                    padding: 10px 14px;
                    background: #0d1117;
                    border: 1px dashed #1e3a5f;
                    border-radius: 6px;
                    color: #cbd5e1;
                    font-size: 11px;
                    line-height: 1.55;
                }
                .col-converge-header {
                    color: #60a5fa; font-weight: 700;
                    font-size: 10px; text-transform: uppercase;
                    letter-spacing: 0.8px; margin-bottom: 4px;
                }
                .col-converge code {
                    background: #131a2e; color: #e0e0e0;
                    padding: 1px 6px; border-radius: 3px;
                    font-family: 'SF Mono', Consolas, monospace;
                    font-size: 10px;
                }
                .col-converge-arrow {
                    text-align: center; color: #3b82f6;
                    font-size: 18px; margin: 4px 0;
                }
            </style>
            """

            def _chain_with_source(target_model: str, target_column: str):
                """Return a source→...→target ordered list (or None)."""
                chain = _trace(target_model, target_column)
                if not chain:
                    return None
                chain_display = list(reversed(chain))
                if chain_display and chain_display[0].get('layer') == 'staging':
                    stg = str(chain_display[0].get('model', '') or '')
                    if stg.startswith('stg_sap__'):
                        sap_table = stg.replace('stg_sap__', '', 1).upper()
                        sap_col = str(chain_display[0].get('column', '') or '').upper()
                        chain_display.insert(0, {
                            'layer': 'source', 'model': sap_table, 'column': sap_col,
                            'expression': '', 'transformation': 'raw',
                            'origin_table': '', 'origin_column': '',
                        })
                return chain_display

            def _render_node(step):
                layer = step.get('layer') or 'other'
                accent = _layer_accent.get(layer, '#6b7280')
                transform = step.get('transformation') or 'direct'
                return (
                    f'<div class="col-node" style="border-top:3px solid {accent}">'
                    f'<div class="col-node-layer" style="color:{accent}">{_esc(layer)}</div>'
                    f'<div class="col-node-model">{_esc(step["model"])}</div>'
                    f'<div class="col-node-column">.{_esc(step["column"])}</div>'
                    f'<div class="col-node-transform">{_esc(transform)}</div>'
                    '</div>'
                )

            def _render_row(chain_display):
                parts = ['<div class="col-lineage-row">']
                for i, step in enumerate(chain_display):
                    parts.append(_render_node(step))
                    if i < len(chain_display) - 1:
                        parts.append('<div class="col-arrow">→</div>')
                parts.append('</div>')
                return parts

            def _detect_upstream_targets(
                target_model: str,
                target_column: str,
                s2t_row,
                all_rows,
            ):
                """Find other target columns in the same mart that are referenced
                in this column's expression or plain/SQL rule.

                A calculated column like `lead_time_days = po_date - gr_date`
                will reference `po_date` and `first_gr_date` — both of which
                are other target columns in the same s2t set. Those become
                upstream branches.
                """
                peers = (
                    all_rows[
                        (all_rows['target_model'] == target_model)
                        & (all_rows['target_column'] != target_column)
                    ]['target_column']
                    .dropna()
                    .unique()
                    .tolist()
                )
                if not peers:
                    return []

                expr_sources = []
                _v_sql = s2t_row.get('transformation_logic_sql', '')
                expr_sources.append(str(_v_sql).strip() if pd.notna(_v_sql) else '')
                _v_plain = s2t_row.get('transformation_logic_plain', '')
                expr_sources.append(str(_v_plain).strip() if pd.notna(_v_plain) else '')
                cl_row = _find_col(target_model, target_column)
                if cl_row is not None:
                    _v_expr = cl_row.get('expression', '')
                    expr_sources.append(str(_v_expr).strip() if pd.notna(_v_expr) else '')
                blob = " ".join(expr_sources)
                if not blob.strip():
                    return []

                upstream = []
                for peer in peers:
                    if not peer:
                        continue
                    # Match as a whole word so `po_date` doesn't match inside
                    # `total_po_date_count` etc.
                    if re.search(r'\b' + re.escape(peer) + r'\b', blob, re.IGNORECASE):
                        upstream.append(peer)
                return upstream

            def _render_col_chain(
                target_model: str,
                target_column: str,
                s2t_row=None,
                all_rows=None,
            ):
                """Return (html, height_px) for a target column's lineage.

                When the column is calculated from multiple other target
                columns in the same s2t set (e.g. lead_time_days uses
                po_date and first_gr_date), render one horizontal chain per
                upstream branch, then a convergence block with join/filter
                context, then the final target node.
                """
                if col_lineage_df.empty:
                    return (
                        _col_lineage_css
                        + '<div class="col-lineage-wrap"><div class="col-empty">'
                        "Column lineage not yet scanned for this project."
                        "</div></div>"
                    ), 60

                chain_display = _chain_with_source(target_model, target_column)
                if chain_display is None:
                    return (
                        _col_lineage_css
                        + '<div class="col-lineage-wrap"><div class="col-empty">'
                        f"No column lineage recorded for {_esc(target_model)}."
                        f"{_esc(target_column)}."
                        "</div></div>"
                    ), 60

                upstream = []
                if s2t_row is not None and all_rows is not None:
                    upstream = _detect_upstream_targets(
                        target_model, target_column, s2t_row, all_rows,
                    )

                parts = [_col_lineage_css, '<div class="col-lineage-wrap">']

                if not upstream:
                    # Simple case — single horizontal chain
                    parts.extend(_render_row(chain_display))
                    parts.append('</div>')
                    return '\n'.join(parts), 130

                # Multi-branch: render one chain per upstream target, then
                # a convergence block, then the final target node
                for up in upstream:
                    up_chain = _chain_with_source(target_model, up)
                    parts.append(
                        f'<div class="col-branch-label">input: <strong>{_esc(up)}</strong></div>'
                    )
                    if up_chain is None:
                        parts.append(
                            '<div class="col-empty">'
                            f"(no lineage recorded for {_esc(up)})"
                            "</div>"
                        )
                        continue
                    parts.extend(_render_row(up_chain))

                # Convergence block with join / filter context
                conv = ['<div class="col-converge">']
                conv.append('<div class="col-converge-header">Join / Filter applied</div>')
                rendered_any = False
                if s2t_row is not None:
                    _raw_jd = s2t_row.get('join_description', '')
                    join_desc = str(_raw_jd).strip() if pd.notna(_raw_jd) else ''
                    # Only render "JOIN:" for actual join conditions (has
                    # an '=' somewhere). Source-table descriptions are
                    # redundant with the lineage boxes above.
                    if str(join_desc).strip() and '=' in str(join_desc):
                        conv.append(
                            f'<div><strong>JOIN:</strong> {_esc(join_desc)}</div>'
                        )
                        rendered_any = True
                    _raw_fd = s2t_row.get('filter_description', '')
                    filter_desc = str(_raw_fd).strip() if pd.notna(_raw_fd) else ''
                    if str(filter_desc).strip():
                        conv.append(
                            f'<div><strong>FILTER:</strong> {_esc(filter_desc)}</div>'
                        )
                        rendered_any = True
                if not rendered_any:
                    conv.append(
                        '<div><em>(no explicit join/filter recorded in the S2T mapping)</em></div>'
                    )
                conv.append('</div>')
                conv.append('<div class="col-converge-arrow">↓</div>')
                parts.extend(conv)

                # Calculation box — show the actual SQL expression that
                # combines the upstream inputs. Only renders in the
                # multi-branch path (simple direct mappings skip it) and
                # only when s2t_mapping.transformation_logic_sql is set.
                calc_sql = ""
                if s2t_row is not None:
                    _raw_cs = s2t_row.get('transformation_logic_sql', '')
                    calc_sql = str(_raw_cs).strip() if pd.notna(_raw_cs) else ''
                calc_height = 0
                if calc_sql:
                    parts.append(
                        '<div style="display:block; text-align:center; '
                        'padding:2px 0; overflow:visible;">'
                        '<div class="col-node" '
                        'style="display:inline-block; text-align:left; '
                        'border-left:3px solid #f59e0b; min-width:auto; '
                        'max-width:none; width:auto;">'
                        '<div class="col-node-layer" style="color:#f59e0b;">CALCULATION</div>'
                        f'<div class="col-node-column" '
                        f'style="font-size:12px; white-space:normal; '
                        f'overflow:visible; text-overflow:clip;">'
                        f'{_esc(calc_sql)}</div>'
                        '</div></div>'
                    )
                    parts.append('<div class="col-converge-arrow">↓</div>')
                    calc_height = 90

                # Final target node alone on its own row. When a
                # CALCULATION box is rendered above, the transformation is
                # already shown there, so relabel the final node to
                # "output" to avoid the misleading "case_when" tag with
                # no SQL attached.
                final_node = dict(chain_display[-1])  # marts-layer entry (copy)
                if calc_sql:
                    final_node['transformation'] = 'output'
                parts.append(
                    '<div style="display:block; text-align:center; '
                    'padding:2px 0; overflow:visible;">'
                )
                # _render_node returns a div; wrap it in an inline-block shell
                # so text-align:center on the parent centers it.
                parts.append(
                    '<div style="display:inline-block; text-align:left;">'
                    + _render_node(final_node)
                    + '</div>'
                )
                parts.append('</div>')

                parts.append('</div>')  # /col-lineage-wrap

                # Tightened height budget — previous formula over-estimated
                # and left a large blank gap after the final node.
                # ~110 per upstream chain (label + row) + ~100 for
                # Join/Filter block + calc + ~95 for final node + padding
                height = 30 + len(upstream) * 110 + 100 + calc_height + 95
                return '\n'.join(parts), height

            # Shrink the font on every st.code() block rendered inside
            # this tab so long CASE WHEN expressions fit the narrow
            # right column without horizontal scrolling. We keep
            # st.code() itself (instead of a custom <div>) so we get
            # Streamlit's built-in SQL syntax highlighting for free.
            st.markdown(
                """
                <style>
                div[data-testid="stCode"] code,
                div[data-testid="stCode"] pre,
                div.stCode code,
                div.stCode pre {
                    font-size: 11px !important;
                    line-height: 1.4 !important;
                    white-space: pre-wrap !important;
                    word-break: break-word !important;
                }
                </style>
                """,
                unsafe_allow_html=True,
            )

            # --- Build one expander per (target_model, target_column) ---
            ordered_s2t = (
                term_s2t
                .dropna(subset=['target_column'])
                .drop_duplicates(subset=['target_model', 'target_column'], keep='first')
            )

            if ordered_s2t.empty:
                st.caption("No target columns mapped yet for this term.")

            for _, row in ordered_s2t.iterrows():
                _raw_tm = row.get('target_model', '')
                target_model = str(_raw_tm).strip() if pd.notna(_raw_tm) else ''
                _raw_tc = row.get('target_column', '')
                target_col = str(_raw_tc).strip() if pd.notna(_raw_tc) else ''
                if not target_col:
                    continue

                header = f"**{target_model}.{target_col}**" if target_model else f"**{target_col}**"
                with st.expander(header, expanded=False):
                    left, right = st.columns([1, 1])

                    with left:
                        st.markdown("**Business Rule**")
                        logic = row.get('transformation_logic_plain', '')
                        _raw_notes = row.get('notes', '')
                        notes = str(_raw_notes).strip() if pd.notna(_raw_notes) else ''
                        is_placeholder = 'needs business rule' in notes.lower()
                        if pd.notna(logic) and str(logic).strip():
                            st.markdown(str(logic).strip())
                        elif is_placeholder:
                            st.warning(
                                "Business rule missing -- author this in "
                                "the glossary edit form or S2T mapping CSV."
                            )
                        else:
                            st.caption("_(no plain-language rule captured)_")

                        # Only render "Join:" when the text contains an
                        # actual join condition (has '='). Source-table
                        # descriptions like "Header of material document"
                        # or "Header table — one row per PO" are redundant
                        # with the lineage boxes and were misleading as
                        # joins.
                        join_desc = row.get('join_description', '')
                        if (pd.notna(join_desc) and str(join_desc).strip()
                                and '=' in str(join_desc)):
                            st.caption(f"↳ Join: {join_desc}")
                        filter_desc = row.get('filter_description', '')
                        if pd.notna(filter_desc) and str(filter_desc).strip():
                            st.caption(f"↳ Filter: {filter_desc}")

                    with right:
                        # Prefer the ACTUAL dbt SQL from the column lineage
                        # catalog — it's the code really running against the
                        # database. Walk the full trace for this column and
                        # pick the deepest non-direct expression; that's where
                        # the real transformation lives (e.g. the staging-layer
                        # CASE WHEN that parses SAP's YYYYMMDD text into DATE).
                        # Only fall back to s2t_mapping.transformation_logic_sql
                        # if the column lineage has nothing useful — the S2T
                        # mapping SQL is often an idealised summary that
                        # doesn't match what actually runs.
                        chain = (
                            _trace(target_model, target_col)
                            if not col_lineage_df.empty
                            else []
                        )
                        actual_sql = ""
                        all_direct = bool(chain) and all(
                            (str(s.get('transformation') or '').strip() or 'direct') == 'direct'
                            for s in chain
                        )
                        for s in chain:
                            expr = str(s.get('expression', '') or '').strip()
                            transform = (s.get('transformation') or '').strip()
                            if expr and transform != 'direct':
                                actual_sql = expr
                        fallback = row.get('transformation_logic_sql', '')
                        fallback_str = str(fallback).strip() if pd.notna(fallback) else ""
                        if not actual_sql and fallback_str:
                            actual_sql = fallback_str

                        st.markdown("**Actual SQL (from dbt model)**")
                        if actual_sql:
                            # st.code() gives us SQL syntax highlighting
                            # out of the box; the CSS block at the top of
                            # this tab shrinks its font so long CASE WHEN
                            # expressions fit the column.
                            st.code(actual_sql, language="sql")
                        elif all_direct and not fallback_str:
                            # Entire trace is direct pass-throughs and the
                            # s2t_mapping SQL was cleared by sync as a
                            # truthful "no transformation" signal. Say so
                            # explicitly rather than leaving a silent empty
                            # block.
                            st.caption(
                                "_Direct pass-through (no transformation). "
                                "The column carries the upstream value unchanged "
                                "through every layer of the pipeline._"
                            )
                        else:
                            st.caption("_(no SQL captured)_")

                    # Show transformation chain steps if available
                    cl_row_for_chain = _find_col(target_model, target_col)
                    if cl_row_for_chain is not None:
                        t_chain_raw = cl_row_for_chain.get('transformation_chain', '')
                        t_chain = str(t_chain_raw or '') if pd.notna(t_chain_raw) else ''
                        t_chain_plain_raw = cl_row_for_chain.get('transformation_chain_plain', '')
                        t_chain_plain = str(t_chain_plain_raw or '') if pd.notna(t_chain_plain_raw) else ''
                        if t_chain.strip() and t_chain.strip() != 'nan':
                            steps = [s.strip() for s in t_chain.split(';') if s.strip() and s.strip() != 'nan']
                            plain_steps = [s.strip() for s in t_chain_plain.split(';') if s.strip()] if t_chain_plain.strip() and t_chain_plain.strip() != 'nan' else []
                            if steps:
                                st.markdown("**Transformation Steps**")
                                for si, step in enumerate(steps):
                                    plain = plain_steps[si] if si < len(plain_steps) and plain_steps[si] != 'nan' else ""
                                    label = f"_{plain}_" if plain else ""
                                    st.caption(f"Step {si+1}: `{step[:80]}` {label}")

                    # Bottom strip: full lineage.
                    # For calculated columns (one that references peer target
                    # columns in its SQL), render one upstream chain per input
                    # plus a convergence block showing the join/filter. For
                    # simple direct mappings, keep a single horizontal chain.
                    st.markdown("**Lineage**")
                    chain_html, chain_height = _render_col_chain(
                        target_model, target_col,
                        s2t_row=row,
                        all_rows=term_s2t,
                    )
                    st.components.v1.html(chain_html, height=chain_height, scrolling=True)

            # --- ABAP programs ---
            if not abap_meta.empty:
                src_upper = [t.upper() for t in lin['source_tables']]
                relevant_abap = abap_meta[
                    abap_meta['tables_read'].apply(
                        lambda x: any(t in str(x or '').upper() for t in src_upper)
                    )
                ]
                if not relevant_abap.empty:
                    with st.expander(
                        f"⚠️ ABAP Custom Code ({len(relevant_abap)} programs touch source tables)",
                        expanded=False,
                    ):
                        risk_colors = {"critical": "#f87171", "high": "#fbbf24",
                                       "medium": "#60a5fa", "low": "#4ade80"}
                        for _, prog in relevant_abap.iterrows():
                            _raw_risk = prog.get('risk_level', '')
                            risk = str(_raw_risk).strip() if pd.notna(_raw_risk) else ''
                            color = risk_colors.get(risk, "#6b7280")
                            _raw_desc2 = prog.get('description', '')
                            desc = (str(_raw_desc2).strip() if pd.notna(_raw_desc2) else '')[:180]
                            st.markdown(
                                f"**`{prog['program_name']}`** "
                                f"<span style='color:{color}'>[{risk.upper()}]</span> — {desc}",
                                unsafe_allow_html=True,
                            )
                            _raw_rule = prog.get('business_rule_plain', '')
                            rule = (str(_raw_rule).strip() if pd.notna(_raw_rule) else '')[:250]
                            if rule:
                                st.caption(f"Business rule: {rule}")

            target_display = term_s2t[['target_model', 'target_column']].dropna().drop_duplicates()
            if not target_display.empty:
                st.markdown("#### 🎯 Target")
                st.dataframe(target_display, use_container_width=True, hide_index=True)

            if lin["dashboard_pages"]:
                st.markdown(f"**Displayed on:** {', '.join(lin['dashboard_pages'])}")

            st.divider()

            st.divider()

            # --- FULL profiling ---
            st.subheader("📊 Data Profiling — Full Detail")
            render_full_profile(get_connection(), term_s2t)

            # KI #71 — strict-cascade archive with guided unwind.
            # All UI/state logic lives in _archive_ui.render_archive_section.
            from _archive_ui import render_archive_section
            render_archive_section(term_id, term, term_s2t)


# ============================================================
# TAB 4: DATA QUALITY — plain-language DQ rules → Claude → dbt test
# ============================================================
with tab_dq:
    st.subheader("🛡️ Data Quality Rules")
    st.caption("Define data quality rules in plain language. Claude generates the dbt test.")

    if not term_names:
        st.info("No business terms defined yet.")
    else:
        selected_name = st.selectbox(
            "Select Business Term",
            term_names,
            key="term_sel_dq",
            on_change=_sync_from_dq,
        )

        term = _resolve_active_term(selected_name)
        term_id = term['id']
        term_s2t_data = s2t[s2t['business_term_id'] == term_id]

        st.markdown(f"##### 📝 {term['display_name']}")
        st.caption(f"**Definition:** {term['definition']}")

        target_models_list = term_s2t_data['target_model'].dropna().unique().tolist()
        target_model = target_models_list[0] if target_models_list else ''
        target_column = (
            term_s2t_data['target_column'].dropna().iloc[0]
            if not term_s2t_data['target_column'].dropna().empty else ''
        )

        if target_model:
            st.caption(f"**Target model:** `{target_model}` · **Column:** `{target_column}`")
        else:
            st.warning(
                "No S2T mapping for this term yet — generated tests will have nothing to reference. "
                "Define the mapping in the S2T Specification tab first."
            )

        st.divider()

        # --- Existing DQ tests that touch this term's target model ---
        st.markdown("### Existing DQ Tests")
        tests_dir = SEED_DIR.parent / "tests"
        test_files = sorted(tests_dir.glob("*.sql")) if tests_dir.exists() else []

        matching_tests = []
        if target_models_list:
            for tf in test_files:
                try:
                    content = tf.read_text(encoding='utf-8')
                except Exception:
                    continue
                if any(tm in content for tm in target_models_list):
                    matching_tests.append((tf, content))

        if matching_tests:
            _dq_conn = get_connection()
            _dq_results = []
            for tf, content in matching_tests:
                passed, violations, error = execute_dq_test(content, _dq_conn)
                _dq_results.append((tf, content, passed, violations, error))

            # Summary bar
            _total = len(_dq_results)
            _passing = sum(1 for _, _, p, _, e in _dq_results if p and not e)
            _failing = _total - _passing
            if _failing == 0:
                st.success(f"All {_total} tests pass")
            else:
                st.error(f"{_failing} of {_total} tests have violations")

            for tf, content, passed, violations, error in _dq_results:
                if error:
                    icon_label = f"⚠️ {tf.stem} — Error"
                elif passed:
                    icon_label = f"✅ {tf.stem} — PASS"
                else:
                    icon_label = f"❌ {tf.stem} — FAIL ({violations:,} violations)"
                with st.expander(icon_label):
                    st.code(content, language="sql")
                    if error:
                        st.error(f"Execution error: {error[:200]}")
                    elif not passed and violations > 0:
                        st.caption(f"{violations:,} violating rows found")
        else:
            st.caption(
                f"No DQ tests found that reference this term's target model"
                f"{' (`' + target_model + '`)' if target_model else ''}."
            )

        st.divider()

        # --- Create new DQ rule ---
        st.markdown("### Create New DQ Rule")

        dq_rule = st.text_area(
            "Describe the data quality rule in plain language",
            placeholder=(
                "e.g., Lead time cannot be negative\n"
                "e.g., No PO value should exceed €500,000\n"
                "e.g., Every vendor must have at least one PO per quarter"
            ),
            height=110,
            key=f"dq_rule_{term_id}",
        )

        severity = st.radio(
            "Severity",
            ["error (blocks pipeline)", "warn (alert only)"],
            key=f"dq_severity_{term_id}",
            horizontal=True,
        )

        if st.button(
            "🤖 Create DQ Rule with Claude",
            key=f"create_dq_{term_id}",
            type="primary",
            disabled=not (dq_rule and dq_rule.strip()),
        ):
            if not target_model:
                st.error(
                    "Cannot generate a test without a target model. "
                    "Map this term in the S2T Specification tab first."
                )
            else:
                with st.spinner("Claude is generating the dbt test..."):
                    from claude_api import generate_dq_test

                    available_cols = ""
                    dq_conn = get_connection()
                    for schema in ['main_marts', 'main_obt', 'main_knowledge']:
                        try:
                            cols_df = dq_conn.execute(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
                                [schema, target_model],
                            ).fetchdf()
                            if not cols_df.empty:
                                available_cols = ", ".join(cols_df['column_name'].tolist())
                                break
                        except Exception:
                            continue

                    result = generate_dq_test(
                        term_name=term['display_name'],
                        term_definition=term['definition'],
                        dq_rule_description=dq_rule.strip(),
                        target_model=target_model,
                        target_column=target_column,
                        existing_columns=available_cols,
                    )

                if result and "error" not in result:
                    st.session_state[f"dq_result_{term_id}"] = result
                    # Stash the rule + severity so Save can reference them even after rerun
                    st.session_state[f"dq_rule_saved_{term_id}"] = dq_rule.strip()
                    st.session_state[f"dq_sev_saved_{term_id}"] = severity
                elif result:
                    st.error(f"Claude API error: {result.get('error', 'Unknown error')}")
                    if 'raw_response' in result:
                        with st.expander("Raw response"):
                            st.code(result['raw_response'])

        # --- Generated test review ---
        dq_result = st.session_state.get(f"dq_result_{term_id}")
        if dq_result and "error" not in dq_result:
            st.success("✅ Claude generated the DQ test")

            test_name = str(dq_result.get('test_name') or 'custom_dq_test').strip()
            # Sanitize test_name to be a safe filename (snake_case, alnum + underscore)
            import re as _re
            safe_test_name = _re.sub(r'[^a-zA-Z0-9_]+', '_', test_name).strip('_') or 'custom_dq_test'

            test_sql_default = str(dq_result.get('test_sql') or '').strip()
            description = str(dq_result.get('description') or '').strip()
            explanation = str(dq_result.get('explanation') or '').strip()
            claude_severity = str(dq_result.get('severity') or '').strip().lower()

            st.markdown(f"**Test name:** `{safe_test_name}`")
            if description:
                st.markdown(f"**Description:** {description}")
            if explanation:
                st.info(f"💡 {explanation}")
            if claude_severity in ('error', 'warn'):
                st.caption(f"Claude recommends severity: **{claude_severity}**")

            edited_sql = st.text_area(
                "Edit test SQL if needed",
                value=test_sql_default,
                height=220,
                key=f"edit_dq_{term_id}",
            )

            if st.button("💾 Save DQ Test", key=f"save_dq_{term_id}", type="primary"):
                saved_rule = st.session_state.get(f"dq_rule_saved_{term_id}", '')
                saved_sev = st.session_state.get(f"dq_sev_saved_{term_id}", severity)
                sev_token = "warn" if "warn" in saved_sev else "error"

                test_path = tests_dir / f"{safe_test_name}.sql"
                header = (
                    f"-- DQ Rule: {saved_rule}\n"
                    f"-- Business Term: {term['display_name']} ({term_id})\n"
                    f"-- Target: {target_model}.{target_column}\n"
                    f"-- Severity: {sev_token}\n"
                    f"-- Generated by Claude API\n\n"
                )
                tests_dir.mkdir(parents=True, exist_ok=True)
                with open(test_path, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(header + edited_sql.strip() + "\n")

                st.success(f"✅ Rule saved as `{safe_test_name}`.")
                sync_tests(
                    test_selector=safe_test_name,
                    success_msg=f"✅ Rule `{safe_test_name}` is now active and passing against live data.",
                    spinner_msg=f"Validating `{safe_test_name}` against live data...",
                )

                # Clean up session state so the form resets
                for k in (
                    f"dq_result_{term_id}",
                    f"dq_rule_saved_{term_id}",
                    f"dq_sev_saved_{term_id}",
                ):
                    st.session_state.pop(k, None)
                st.rerun()


# ============================================================
# TAB 5: NEW TERM
# ============================================================
with tab_new:
    st.subheader("Create New Business Term")
    st.caption("Define a new business term. After creation, a data analyst will map it to source tables.")

    with st.form("new_term_form"):
        col1, col2 = st.columns(2)

        with col1:
            new_term_name = st.text_input("Term Name (snake_case)", placeholder="e.g., avg_vendor_lead_time")
            new_display_name = st.text_input("Display Name", placeholder="e.g., Average Vendor Lead Time")
            new_definition = st.text_area("Definition", placeholder="Describe what this metric means, how it should be calculated, and any exclusions...")
            new_unit = st.text_input("Unit", placeholder="e.g., days, percent, EUR, ratio")
            new_grain = st.text_input("Grain", placeholder="e.g., vendor x month, material x quarter")

        with col2:
            new_domain = st.selectbox("Domain", ["procurement", "quality", "inventory", "equipment", "cost_analysis"])
            new_owner = st.text_input("Owner (department)", placeholder="e.g., Procurement Department")
            new_approved_by = st.text_input("To be approved by", placeholder="e.g., Head of Supply Chain")
            new_related = st.text_input("Related terms (semicolon-separated)", placeholder="e.g., on_time_delivery_rate;vendor_scorecard")
            new_notes = st.text_area("Notes", placeholder="Any additional context, exceptions, or business rules...")

        submitted = st.form_submit_button("Create Term")

        if submitted:
            if not new_term_name or not new_display_name or not new_definition:
                st.error("Term name, display name, and definition are required.")
            else:
                # Rule 33: composite uniqueness is
                # (term_name) WHERE status != 'archived'. Allows a new
                # active term to reuse a name whose prior instance is
                # archived, but still blocks duplicates among live terms.
                # Stage D.1: centralized via `filter_active_terms`.
                _active = filter_active_terms(glossary)
                conflict = _active[
                    _active['term_name'].astype(str).str.lower()
                    == str(new_term_name).strip().lower()
                ]
                if not conflict.empty:
                    st.error(
                        f"A non-archived term with the name '{new_term_name}' "
                        f"already exists (id={conflict.iloc[0]['id']}). "
                        f"Archive it first if you want to redefine."
                    )
                else:
                    max_id = glossary['id'].apply(
                        lambda x: int(x.replace('BG', '')) if isinstance(x, str) and x.startswith('BG') else 0
                    ).max()
                    new_id = f"BG{int(max_id) + 1:03d}"

                    csv_path = SEED_DIR / "business_glossary.csv"
                    # Match the full 19-column schema (4 archive columns
                    # on top of the 15 original fields).
                    with open(csv_path, 'a', encoding='utf-8', newline='') as f:
                        writer = csv.writer(f, lineterminator='\n')
                        writer.writerow([
                            new_id, new_term_name, new_display_name, new_definition,
                            new_unit, new_grain, new_owner, new_approved_by,
                            'draft', new_domain, new_related, new_notes,
                            pd.Timestamp.now().strftime('%Y-%m-%d'),
                            '', '',                 # business_join/filter descriptions
                            '', '', '', '',         # archive_id + 3 archive_* fields
                        ])
                    sync_seed(
                        "business_glossary",
                        success_msg=(
                            f"✅ Term '{new_display_name}' created as {new_id} (draft) "
                            "and synced to the database."
                        ),
                    )
                    st.info(
                        "Next step: a data analyst will map this term to source data in the "
                        "**🔧 S2T Specification** tab."
                    )
