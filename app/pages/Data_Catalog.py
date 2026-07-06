"""Data Catalog — architecture, Data Vault design, ABAP catalog, SAP data dictionary, Source Diagnostic."""
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import os

import streamlit as st

# Active raw source schema (env DG_SOURCE_SCHEMA, default raw_sap).
_ACTIVE_SRC = os.environ.get("DG_SOURCE_SCHEMA", "raw_sap")
import pandas as pd
import duckdb

from db import query, get_connection
from vault_docs import describe_vault_column
from _analyzer_registry import SOURCE_DIAGNOSTIC_ANALYZERS
from _dar_render import render_dar_card
from _data_catalog_helpers import (
    resolve_table_detail_default_index,
    format_batch_error_expander,
    has_semantic_model_row,  # noqa: F401  (kept for future panel callers)
    parse_compile_skip_reason,
)

DB_PATH = Path(__file__).resolve().parent.parent.parent / "cpe_analytics.duckdb"

st.title("🔗 Data Catalog")
st.caption("Architecture · Data Vault design · ABAP custom code · SAP data dictionary")
st.divider()

# Load shared metadata once — all tabs reference these for cross-linking
sap_dict = query("SELECT * FROM main_seeds.sap_data_dictionary ORDER BY table_name, field_name")
abap = query("SELECT * FROM main_seeds.abap_logic_catalog ORDER BY risk_level, id")
z_tables = query("SELECT * FROM main_seeds.z_tables_catalog ORDER BY id")


tab_arch, tab_dv, tab_abap, tab_dict, tab_diag = st.tabs([
    "🏗️ Architecture",
    "🏛️ Data Vault",
    "⚙️ ABAP Catalog",
    "📚 SAP Dictionary",
    "🔍 Source Diagnostic",
])

# ============================================================
# TAB 1: Architecture Overview
# ============================================================
with tab_arch:
    st.subheader("Data Architecture Layers")

    pipeline = query("SELECT * FROM main_knowledge.knowledge_pipeline_summary")
    pipeline_map = dict(zip(pipeline['layer'], pipeline['table_count']))

    layers = [
        {"name": f"Source ({_ACTIVE_SRC})", "key": _ACTIVE_SRC, "color": "#6b7280", "desc": "Raw source tables — as-is from the active source system"},
        {"name": "Staging",     "key": "staging",   "color": "#60a5fa", "desc": "1:1 with source + hash keys, hashdiff, type casting. No joins, no renaming."},
        {"name": "Data Vault",  "key": "vault",     "color": "#a78bfa", "desc": "Hubs (entities) + Links (relationships) + Satellites (attributes with SCD2 history)"},
        {"name": "Marts",       "key": "marts",     "color": "#4ade80", "desc": "Kimball star schema — dimensions and facts. Business-friendly names. Pre-joined."},
        {"name": "OBT Views",   "key": "obt",       "color": "#fbbf24", "desc": "One Big Table views — flattened for Streamlit. One query per dashboard page."},
        {"name": "Knowledge",   "key": "knowledge", "color": "#f87171", "desc": "Computed business facts — procurement health, vendor grades, lifecycle metrics."},
    ]

    for i, layer in enumerate(layers):
        count = pipeline_map.get(layer['key'], '?')
        if count == '?' and layer['key'] == _ACTIVE_SRC:
            # The knowledge pipeline summary only tracks the SAP demo layers;
            # count a non-default active source from the live schema instead.
            try:
                count = int(query(
                    "SELECT COUNT(*) AS n FROM information_schema.tables "
                    f"WHERE table_schema='{_ACTIVE_SRC}'"
                ).iloc[0]["n"])
            except Exception:
                count = '?'
        with st.container():
            col1, col2, col3 = st.columns([1, 3, 1])
            with col1:
                st.markdown(
                    f"<div style='background:{layer['color']};padding:12px;border-radius:8px;text-align:center'>"
                    f"<span style='color:white;font-weight:bold;font-size:20px'>{count}</span><br>"
                    f"<span style='color:white;font-size:11px'>models</span></div>",
                    unsafe_allow_html=True,
                )
            with col2:
                st.markdown(f"**{layer['name']}** (`{layer['key']}`)")
                st.caption(layer['desc'])
            with col3:
                if i < len(layers) - 1:
                    st.markdown("<div style='text-align:center;font-size:24px;color:#6b7280'>↓</div>", unsafe_allow_html=True)

    st.divider()
    total = pipeline['table_count'].sum() if not pipeline.empty else 0
    st.metric("Total Pipeline Objects", f"{total}")

# ============================================================
# TAB 2: Data Vault Design
# ============================================================
with tab_dv:
    st.subheader("Data Vault 2.0 Design")

    dv = query("SELECT * FROM main_seeds.data_vault_design ORDER BY entity_type, id")

    if not dv.empty:
        # Cached in-memory Parquet-backed engine — never opens cpe_analytics.duckdb
        dv_conn = get_connection()
        try:
            for entity_type in ['hub', 'link', 'satellite']:
                entities = dv[dv['entity_type'] == entity_type]
                if not entities.empty:
                    type_label = {
                        "hub": "🔵 Hubs (Business Entities)",
                        "link": "🔗 Links (Relationships)",
                        "satellite": "📎 Satellites (Attributes)",
                    }
                    st.markdown(f"### {type_label.get(entity_type, entity_type)}")

                    for _, ent in entities.iterrows():
                        with st.expander(f"`{ent['entity_name']}` — {ent['notes']}"):
                            st.markdown(f"**Business Key:** `{ent['business_key']}`")
                            st.markdown(f"**Source Tables:** `{ent['source_tables']}`")
                            st.markdown(f"**Grain:** {ent['grain']}")
                            st.markdown(f"**Decided:** {ent['decided_date']}")

                            # Show actual vault model columns from DuckDB (information_schema)
                            vault_table_name = ent['entity_name']
                            try:
                                vault_cols = dv_conn.execute(f"""
                                    SELECT column_name, data_type
                                    FROM information_schema.columns
                                    WHERE table_schema = 'main_vault'
                                      AND table_name = '{vault_table_name}'
                                    ORDER BY ordinal_position
                                """).fetchdf()

                                if not vault_cols.empty:
                                    vault_cols['description'] = vault_cols['column_name'].apply(describe_vault_column)
                                    st.markdown("**Vault Model Columns:**")
                                    st.dataframe(
                                        vault_cols,
                                        use_container_width=True, hide_index=True,
                                        height=min(360, 35 * len(vault_cols) + 38),
                                        column_config={
                                            "column_name": st.column_config.Column("Column", width="medium"),
                                            "data_type":   st.column_config.Column("Type",   width="small"),
                                            "description": st.column_config.Column("Description", width="large"),
                                        },
                                    )
                                    row_count = dv_conn.execute(
                                        f"SELECT COUNT(*) FROM main_vault.{vault_table_name}"
                                    ).fetchone()[0]
                                    st.caption(f"Rows: {row_count:,}")
                                else:
                                    st.caption(
                                        f"⚠️ Designed but not yet built — `main_vault.{vault_table_name}` does not exist."
                                    )
                            except Exception as e:
                                st.caption(f"Could not load vault columns: {e}")
        finally:
            pass  # cached in-memory connection — do not close
    else:
        st.info("No Data Vault design documented yet.")

# ============================================================
# TAB 3: ABAP Catalog
# ============================================================
with tab_abap:
    st.subheader("ABAP Custom Code Catalog")
    st.caption("Custom programs, user exits, BAdIs, and enhancements in HT's SAP system")

    if not abap.empty:
        risk_filter = st.multiselect(
            "Filter by Risk Level",
            ['critical', 'high', 'medium', 'low'],
            default=['critical', 'high'],
        )
        filtered = abap[abap['risk_level'].isin(risk_filter)] if risk_filter else abap

        for _, prog in filtered.iterrows():
            risk_colors = {"critical": "#f87171", "high": "#fbbf24", "medium": "#60a5fa", "low": "#4ade80"}
            color = risk_colors.get(prog['risk_level'], "#6b7280")

            with st.expander(f"{prog['program_name']} [{prog['risk_level'].upper()}] — {prog['program_type']}"):
                st.markdown(f"**Description:** {prog['description']}")
                st.markdown(f"**Reads:** `{prog['tables_read']}`")
                st.markdown(f"**Writes:** `{prog['tables_written']}`")
                st.markdown(f"**Business Rule:** {prog['business_rule_plain']}")
                st.code(prog['business_rule_condition'], language="abap")
                st.caption(
                    f"Module: {prog['module']} | Transaction: {prog['transaction']} | "
                    f"Last changed: {prog['last_changed_date']} by {prog['last_changed_by']}"
                )
                if pd.notna(prog.get('notes')) and prog['notes']:
                    st.info(f"Notes: {prog['notes']}")

                # Cross-reference: SAP dictionary for standard tables this program touches
                _raw_tr = prog.get('tables_read', '')
                tables_read_str = str(_raw_tr).strip() if pd.notna(_raw_tr) else ''
                _raw_tw = prog.get('tables_written', '')
                tables_written_str = str(_raw_tw).strip() if pd.notna(_raw_tw) else ''
                all_tables = [
                    t.strip().upper()
                    for t in (tables_read_str + ';' + tables_written_str).split(';')
                    if t.strip()
                ]
                standard_tables = sorted({t for t in all_tables if not t.startswith('Z')})
                z_table_names = sorted({t for t in all_tables if t.startswith('Z')})

                if standard_tables and not sap_dict.empty:
                    relevant_fields = sap_dict[sap_dict['table_name'].isin(standard_tables)]
                    if not relevant_fields.empty:
                        with st.expander(
                            f"📚 SAP fields touched ({len(relevant_fields)} fields across {len(standard_tables)} tables)"
                        ):
                            st.dataframe(
                                relevant_fields[[
                                    'table_name', 'field_name', 'data_type',
                                    'description_en', 'business_meaning'
                                ]].sort_values(['table_name', 'field_name']),
                                use_container_width=True, hide_index=True,
                            )

                if z_table_names and not z_tables.empty:
                    relevant_z = z_tables[z_tables['table_name'].isin(z_table_names)]
                    if not relevant_z.empty:
                        with st.expander(f"🔧 Custom Z-tables ({len(relevant_z)} tables)"):
                            for _, zt in relevant_z.iterrows():
                                st.markdown(
                                    f"**`{zt['table_name']}`** — {zt['description']}\n\n"
                                    f"Key fields: `{zt['key_fields']}`\n\n"
                                    f"Important fields: `{zt['important_fields']}`\n\n"
                                    f"Maintained by: {zt['maintained_by']} via {zt['maintenance_transaction']}\n\n"
                                    f"Refresh: {zt['refresh_frequency']} · ~{zt['rows_estimate']} rows"
                                )
                                st.divider()

        st.divider()
        st.markdown("### Custom Z-Tables")
        if not z_tables.empty:
            for _, zt in z_tables.iterrows():
                referencing = zt.get('referenced_by_programs', '')
                ref_list = [r.strip() for r in str(referencing).split(';') if r.strip()]

                with st.expander(
                    f"`{zt['table_name']}` — {zt['description']} (referenced by {len(ref_list)} programs)"
                ):
                    col1, col2 = st.columns(2)
                    with col1:
                        st.markdown(f"**Key fields:** `{zt['key_fields']}`")
                        st.markdown(f"**Important fields:** `{zt['important_fields']}`")
                        st.markdown(f"**Estimated rows:** ~{zt['rows_estimate']}")
                    with col2:
                        st.markdown(f"**Maintained by:** {zt['maintained_by']}")
                        st.markdown(f"**Maintenance transaction:** {zt['maintenance_transaction']}")
                        st.markdown(f"**Refresh frequency:** {zt['refresh_frequency']}")

                    if ref_list:
                        st.markdown("**Referenced by programs:**")
                        for ref_id in ref_list:
                            prog_match = abap[abap['id'] == ref_id] if not abap.empty else pd.DataFrame()
                            if not prog_match.empty:
                                p = prog_match.iloc[0]
                                risk_color = {
                                    "critical": "#f87171", "high": "#fbbf24",
                                    "medium": "#60a5fa", "low": "#4ade80"
                                }.get(p['risk_level'], "#6b7280")
                                desc = str(p.get('description') or '')[:100]
                                st.markdown(
                                    f"- `{p['program_name']}` "
                                    f"<span style='color:{risk_color}'>[{p['risk_level'].upper()}]</span> — {desc}",
                                    unsafe_allow_html=True,
                                )
                            else:
                                st.markdown(f"- `{ref_id}`")

                    if pd.notna(zt.get('notes')) and zt['notes']:
                        st.info(f"Notes: {zt['notes']}")

# ============================================================
# TAB 4: SAP Data Dictionary
# ============================================================
with tab_dict:
    st.subheader("SAP Data Dictionary")
    st.caption("Field-level documentation for all SAP MM tables in scope")

    sap_dict_full = sap_dict  # already loaded at top of file

    if not sap_dict_full.empty:
        tables = sap_dict_full['table_name'].unique().tolist()
        selected_tables = st.multiselect("Filter by Table", tables, default=[])

        domains = sap_dict_full['domain_area'].unique().tolist()
        selected_domains = st.multiselect("Filter by Domain", domains, default=[])

        filtered = sap_dict_full
        if selected_tables:
            filtered = filtered[filtered['table_name'].isin(selected_tables)]
        if selected_domains:
            filtered = filtered[filtered['domain_area'].isin(selected_domains)]

        search = st.text_input("Search field name or description", "")
        if search:
            filtered = filtered[
                filtered['field_name'].str.contains(search, case=False, na=False) |
                filtered['description_en'].str.contains(search, case=False, na=False) |
                filtered['business_meaning'].str.contains(search, case=False, na=False)
            ]

        st.markdown(f"**Showing {len(filtered)} of {len(sap_dict_full)} fields**")
        st.dataframe(
            filtered[['table_name', 'field_name', 'data_type', 'length',
                       'description_en', 'business_meaning', 'example_value', 'domain_area']],
            use_container_width=True, hide_index=True, height=500
        )
    else:
        st.info("No SAP data dictionary loaded.")


# ============================================================
# TAB 5: Source Diagnostic (Stage F)
# ============================================================
#
# Three sub-views:
#   A — Ingested Tables  : table list + semantic_model status + DAR coverage
#   B — Table Detail     : per-table semantic_model + schema_discovery + grid
#   C — Catalog Overview : bulk dispatch + summary stats
#
# Sub-view routing via session_state. Click "Detail" on a Row A row sets
# the selected table + view, then st.rerun() switches the display.
# ============================================================

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_FRESHNESS_WARN_DAYS = 30


def _format_age(executed_at_utc_str: str) -> str:
    """Return human-friendly age from an ISO timestamp string."""
    if not executed_at_utc_str:
        return "never"
    try:
        # Handle trailing Z, fractional seconds, tzinfo presence varies
        s = str(executed_at_utc_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return "?"
    now = datetime.now(timezone.utc)
    delta = now - dt
    days = delta.days
    if days < 1:
        hrs = int(delta.total_seconds() // 3600)
        if hrs < 1:
            return "just now"
        return f"{hrs}h ago"
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    return f"{months}mo ago"


def _is_stale(executed_at_utc_str: str, days_threshold: int = _FRESHNESS_WARN_DAYS) -> bool:
    if not executed_at_utc_str:
        return False
    try:
        s = str(executed_at_utc_str).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - dt).days > days_threshold


def _ingested_tables_with_status() -> pd.DataFrame:
    """Stage F EDIT S1: ingested (real) + documented-only (z_tables_catalog).

    Returns DataFrame with columns:
      table_name, ingest_status, row_count, col_count, sem_populated, dar_coverage
    """
    ingested_rows = query(
        "SELECT LOWER(table_name) AS table_name "
        "FROM information_schema.tables "
        f"WHERE table_schema='{_ACTIVE_SRC}' ORDER BY table_name"
    )
    ingested_rows['ingest_status'] = 'ingested'

    # Z-table documentation rows are SAP-domain content; only merge them
    # when the active source IS the SAP schema, so another source's table
    # list isn't padded with foreign documentation-only entries.
    if _ACTIVE_SRC == "raw_sap":
        z_catalog = query(
            "SELECT LOWER(table_name) AS table_name FROM main_seeds.z_tables_catalog"
        )
        # anti-join: keep z-rows not already in ingested set
        ingested_set = set(ingested_rows['table_name'])
        z_not_ingested = z_catalog[~z_catalog['table_name'].isin(ingested_set)].copy()
        z_not_ingested['ingest_status'] = 'documentation_only'
    else:
        z_not_ingested = ingested_rows.iloc[0:0].copy()

    all_tables = pd.concat([ingested_rows, z_not_ingested], ignore_index=True)
    if all_tables.empty:
        return all_tables

    # Row count + col count via a single information_schema query
    col_counts = query(
        "SELECT LOWER(table_name) AS table_name, "
        "       COUNT(*) AS col_count "
        "FROM information_schema.columns "
        f"WHERE table_schema='{_ACTIVE_SRC}' "
        "GROUP BY LOWER(table_name)"
    )
    all_tables = all_tables.merge(col_counts, on='table_name', how='left')
    all_tables['col_count'] = all_tables['col_count'].fillna(0).astype(int)

    # Row counts — one query per ingested table (fast on Parquet views)
    conn = get_connection()
    row_counts: list[int] = []
    for _, r in all_tables.iterrows():
        if r['ingest_status'] != 'ingested':
            row_counts.append(0)
            continue
        try:
            c = conn.execute(
                f'SELECT COUNT(*) FROM {_ACTIVE_SRC}."{r["table_name"]}"'
            ).fetchone()
            row_counts.append(int(c[0]) if c else 0)
        except Exception:  # noqa: BLE001
            row_counts.append(0)
    all_tables['row_count'] = row_counts

    # semantic_model populated (by table)
    sem = query(
        "SELECT LOWER(table_name) AS table_name, 1 AS sem_populated "
        "FROM main_seeds.semantic_model "
        "WHERE populated_by IS NOT NULL AND populated_by != ''"
    )
    all_tables = all_tables.merge(sem, on='table_name', how='left')
    all_tables['sem_populated'] = all_tables['sem_populated'].fillna(0).astype(int)

    # DAR coverage — count distinct analysis_types per table with success/skipped status
    dar = query(
        "SELECT LOWER(source_tables) AS table_name, "
        "       COUNT(DISTINCT analysis_type) AS dar_coverage "
        "FROM main_seeds.domain_analysis_results "
        "WHERE status IN ('success', 'skipped') "
        "GROUP BY LOWER(source_tables)"
    )
    all_tables = all_tables.merge(dar, on='table_name', how='left')
    all_tables['dar_coverage'] = all_tables['dar_coverage'].fillna(0).astype(int)

    return all_tables


def _latest_dars_per_analyzer(table: str) -> dict:
    """Return {analysis_type: {id, status, executed_at_utc, result_json}}
    with the latest DAR per analyzer for this table. Stage F expects
    7 analyzer types in the grid; absent types are mapped to None."""
    try:
        rows = query(
            f"SELECT id, analysis_type, executed_at_utc, status, result_json, query_sql "
            f"FROM main_seeds.domain_analysis_results "
            f"WHERE LOWER(source_tables) = '{table.lower()}' "
            f"ORDER BY executed_at_utc DESC"
        )
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for _, r in rows.iterrows():
        at = r['analysis_type']
        if at in out:
            continue  # keep only latest
        out[at] = {
            'id': r['id'],
            'status': r['status'],
            'executed_at_utc': str(r['executed_at_utc']),
            'result_json': r.get('result_json', ''),
            'query_sql': r.get('query_sql', ''),
            'analysis_type': at,
        }
    return out


def _dispatch_analyzer_subprocess(
    script_rel: str, arg_flavor: str, table: str,
    extra_args: list[str] | None = None,
    timeout: int = 600,
) -> dict:
    """Sequential subprocess dispatch wrapper. Returns result dict with
    returncode, stdout, stderr."""
    script_path = _PROJECT_ROOT / "scripts" / script_rel
    flag = "--tables" if arg_flavor == "plural" else "--table"
    cmd = [sys.executable, str(script_path), flag, table]
    if extra_args:
        cmd.extend(extra_args)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, cwd=str(_PROJECT_ROOT),
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
        }
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "stdout": "", "stderr": f"timeout (>{timeout}s)"}
    except Exception as e:  # noqa: BLE001
        return {"returncode": -2, "stdout": "", "stderr": f"{type(e).__name__}: {e}"}


def _run_source_diagnostic_on_table(table: str) -> list[dict]:
    """View B Run Source Diagnostic: dispatch all 7 analyzers sequentially
    against a single table. Renders progress via st.status."""
    n = len(SOURCE_DIAGNOSTIC_ANALYZERS)
    results: list[dict] = []
    t_start = time.perf_counter()

    with st.status(
        f"Running {n} analyzers on `{table}`...", expanded=True,
    ) as status:
        for i, (script_rel, label, arg_flavor) in enumerate(
            SOURCE_DIAGNOSTIC_ANALYZERS, start=1,
        ):
            status.update(label=f"({i}/{n}) {label} on `{table}`")
            res = _dispatch_analyzer_subprocess(script_rel, arg_flavor, table)
            results.append({**res, "label": label, "script_rel": script_rel})
            if res["returncode"] == 0:
                st.markdown(f"- ✅ **{label}**: ok")
            elif res["returncode"] == -1:
                st.markdown(f"- ⏱️ **{label}**: timeout")
            else:
                st.markdown(f"- ❌ **{label}**: rc={res['returncode']}")

        elapsed = time.perf_counter() - t_start
        successes = sum(1 for r in results if r["returncode"] == 0)
        errors = [r for r in results if r["returncode"] != 0]
        final_state = "complete" if not errors else "error"
        status.update(
            label=(
                f"Source Diagnostic complete on `{table}`: "
                f"{successes}✓ / {len(errors)}✗ / {elapsed:.0f}s"
            ),
            state=final_state,
        )
    return results


def _run_source_diagnostic_on_all(tables: list[str]) -> None:
    """View C bulk dispatch: Pass 1 = 7 analyzers × N tables. Pass 2 =
    bridges_only refresh on each table with schema_discovery DAR."""
    n_tables = len(tables)
    overall_start = time.perf_counter()
    all_successes = 0
    all_errors = 0

    with st.status(
        f"Source Diagnostic on {n_tables} tables — Pass 1 of 2...",
        expanded=True,
    ) as status:
        for ti, table in enumerate(tables, start=1):
            status.update(label=f"Pass 1/2 — ({ti}/{n_tables}) `{table}`")
            errors_here: list[dict] = []
            for script_rel, label, arg_flavor in SOURCE_DIAGNOSTIC_ANALYZERS:
                res = _dispatch_analyzer_subprocess(script_rel, arg_flavor, table)
                if res["returncode"] == 0:
                    all_successes += 1
                else:
                    all_errors += 1
                    errors_here.append({
                        "label": label,
                        "returncode": res["returncode"],
                        "stderr": res.get("stderr", ""),
                    })
            # known_issue #75 fix: surface per-analyzer stderr when any
            # subprocess returned non-zero. Prior behavior swallowed
            # stderr, producing silent batch failures (e.g., objk/mard's
            # ContextDegradedError crashes went undiagnosable from the UI).
            expander = format_batch_error_expander(table, errors_here)
            if expander:
                exp_label, exp_body = expander
                with st.expander(exp_label):
                    st.markdown(exp_body)
                st.markdown(
                    f"- `{table}`: ❌ **{len(errors_here)} error(s)** "
                    f"— stderr expanded above"
                )
            else:
                st.markdown(f"- `{table}`: done")

        status.update(
            label=f"Pass 2/2 — bridges_only refresh across {n_tables} tables",
            state="running",
        )
        for ti, table in enumerate(tables, start=1):
            status.update(
                label=f"Pass 2/2 — ({ti}/{n_tables}) bridges on `{table}`",
            )
            _dispatch_analyzer_subprocess(
                "run_schema_discovery_analysis.py", "singular", table,
                extra_args=["--mode", "bridges_only"],
            )

        elapsed = time.perf_counter() - overall_start
        final_state = "complete" if all_errors == 0 else "error"
        status.update(
            label=(
                f"Source Diagnostic (bulk) complete: "
                f"{all_successes}✓ / {all_errors}✗ / {elapsed:.0f}s"
            ),
            state=final_state,
        )


def _compile_semantic_model_for(table: str) -> dict:
    """Invoke scripts/compile_semantic_model.py --tables <table>. Returns
    result dict from subprocess."""
    return _dispatch_analyzer_subprocess(
        "compile_semantic_model.py",
        arg_flavor="plural",  # --tables is the CLI flag per argparse surface
        table=table,
        timeout=900,
    )


def _render_view_a(tables_df: pd.DataFrame) -> None:
    """View A — Ingested Tables list with status + drill-into-detail buttons."""
    st.subheader("📋 Ingested Tables")
    st.caption(
        f"All {_ACTIVE_SRC} tables + documented-only Z-tables. Click **Detail** on a "
        "row to drill into semantic model + schema discovery evidence."
    )

    if tables_df.empty:
        st.warning(f"No tables found in {_ACTIVE_SRC} or z_tables_catalog.")
        return

    # Summary metrics
    total = len(tables_df)
    ingested = int((tables_df['ingest_status'] == 'ingested').sum())
    docs_only = int((tables_df['ingest_status'] == 'documentation_only').sum())
    sem_count = int(tables_df['sem_populated'].sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total", total)
    c2.metric("Ingested", ingested)
    c3.metric("Docs-only", docs_only)
    c4.metric("Semantic model populated", f"{sem_count} / {ingested}")
    st.divider()

    # Header row
    hdr = st.columns([3, 1, 1, 1, 1, 1, 1])
    hdr[0].markdown("**Table**")
    hdr[1].markdown("**Status**")
    hdr[2].markdown("**Rows**")
    hdr[3].markdown("**Cols**")
    hdr[4].markdown("**Sem**")
    hdr[5].markdown("**DARs**")
    hdr[6].markdown("**Action**")

    # DAR-total = 7 analyzers (6 per-table + schema_discovery).
    # grain_relationship / performance_baseline not counted here since
    # they aren't in the Source Diagnostic registry.
    n_expected_analyzers = len(SOURCE_DIAGNOSTIC_ANALYZERS)

    for _, r in tables_df.iterrows():
        cols = st.columns([3, 1, 1, 1, 1, 1, 1])
        is_ingested = r['ingest_status'] == 'ingested'

        name_label = r['table_name']
        if not is_ingested:
            cols[0].markdown(
                f"<span style='opacity:0.6'>{name_label}</span> "
                "<span style='color:#9ca3af;font-size:11px'>📋 docs-only</span>",
                unsafe_allow_html=True,
            )
        else:
            cols[0].markdown(f"`{name_label}`")

        status_badge = {
            "ingested": "✓",
            "documentation_only": "📋",
        }.get(r['ingest_status'], "?")
        cols[1].markdown(status_badge)
        cols[2].markdown(f"{int(r['row_count']):,}" if is_ingested else "—")
        cols[3].markdown(str(int(r['col_count'])) if r['col_count'] else "—")
        sem_badge = "✅" if r['sem_populated'] else "—"
        cols[4].markdown(sem_badge)
        cols[5].markdown(f"{int(r['dar_coverage'])}/{n_expected_analyzers}")

        if is_ingested:
            if cols[6].button(
                "Detail",
                key=f"detail_{r['table_name']}",
                help="Open Table Detail view",
            ):
                st.session_state['data_catalog_selected_table'] = r['table_name']
                # Option B: one-shot consumed by dispatcher preamble on
                # next rerun to prime the radio widget's key. Writing
                # the widget key directly would hit Streamlit's
                # "cannot modify widget after instantiation" guard
                # because the radio already rendered earlier in this run.
                st.session_state['pending_view_change'] = 'table_detail'
                st.rerun()
        else:
            cols[6].markdown(
                "<span title='Table not loaded into warehouse — ingestion required before diagnostic.' "
                "style='color:#9ca3af;cursor:help'>n/a</span>",
                unsafe_allow_html=True,
            )


def _render_view_b(table: str) -> None:
    """View B — Per-table detail with semantic_model + schema_discovery panels
    + 7-analyzer grid + action buttons."""
    c_head1, c_head2 = st.columns([3, 1])
    c_head1.subheader(f"🔎 {table}")
    if c_head2.button("← Back to Tables", key="back_to_list"):
        st.session_state['pending_view_change'] = 'tables'
        st.rerun()

    # Basic stats
    conn = get_connection()
    try:
        row_count = conn.execute(
            f'SELECT COUNT(*) FROM {_ACTIVE_SRC}."{table}"'
        ).fetchone()[0]
    except Exception:  # noqa: BLE001
        row_count = 0
    col_count = conn.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        f"WHERE table_schema='{_ACTIVE_SRC}' AND LOWER(table_name)=LOWER('{table}')"
    ).fetchone()[0]
    st.caption(f"Schema: `{_ACTIVE_SRC}` · Rows: {row_count:,} · Columns: {col_count}")

    # ── Action buttons ────────────────────────────────────────────
    ca, cb = st.columns(2)
    run_clicked = ca.button(
        "▶ Run Source Diagnostic on This Table",
        type="primary",
        key=f"run_source_diag_{table}",
        help=f"Dispatches {len(SOURCE_DIAGNOSTIC_ANALYZERS)} analyzers sequentially",
    )
    compile_clicked = cb.button(
        "🧱 Compile Semantic Model",
        key=f"compile_sem_{table}",
        help="Invokes scripts/compile_semantic_model.py --tables "
             f"{table} — requires success/skipped DARs for all 7 analyzers",
    )

    if run_clicked:
        _run_source_diagnostic_on_table(table)
        st.success("Diagnostic complete. Refresh the page to see fresh DARs.")
        st.caption("(Streamlit caches DAR data for 60s — hard-refresh your browser.)")

    if compile_clicked:
        with st.spinner(f"Compiling semantic model for `{table}`..."):
            res = _compile_semantic_model_for(table)
        if res['returncode'] == 0:
            # known_issue #78: rc=0 covers both "row written" and
            # "skipped by design". Re-query semantic_model to
            # distinguish; on skip, surface the actual reason from the
            # runner stdout rather than claiming compile wrote a row.
            try:
                _post_rows = conn.execute(
                    "SELECT COUNT(*) FROM main_seeds.semantic_model "
                    f"WHERE LOWER(table_name) = LOWER('{table}')"
                ).fetchone()[0]
            except Exception:  # noqa: BLE001
                _post_rows = 0
            if _post_rows > 0:
                st.success(f"Semantic model compiled for `{table}`.")
            else:
                _reason = parse_compile_skip_reason(res.get('stdout', ''), table)
                if _reason:
                    st.info(
                        f"Skipped: `{table}` — {_reason}. "
                        f"Layer A doesn't cover this table; the term-analysis "
                        f"runner uses the ontology layer (dbt_column_lineage) "
                        f"as the primary context for it."
                    )
                else:
                    st.info(
                        f"Compile returned rc=0 but no row was written for "
                        f"`{table}`. Check the runner output below for the "
                        f"skip reason."
                    )
                with st.expander("compile stdout", expanded=False):
                    st.code(res.get('stdout', '(empty)'), language="text")
        else:
            st.error(
                f"Compile failed rc={res['returncode']}\n\n"
                f"stderr: {res['stderr'][:500]}"
            )

    st.divider()

    # ── Semantic model panel ──────────────────────────────────────
    st.markdown("### 🧱 Semantic Model (Layer A)")
    try:
        sem_rows = conn.execute(
            "SELECT canonical_alias, entity_class, primary_key_cols, "
            "natural_key_cols, typical_join_keys_json, typical_filters, "
            "common_traps, typical_use_cases, review_state, populated_at_utc "
            "FROM main_seeds.semantic_model "
            f"WHERE LOWER(table_name) = LOWER('{table}')"
        ).fetchall()
    except Exception:  # noqa: BLE001
        sem_rows = []
    if not sem_rows:
        # known_issue #79 collapsed the prior 3-way branch (row-exists /
        # ontology-covered-skip / uncovered-empty) into 2-way. Ontology
        # coverage is no longer a compile-skip condition, so "run compile"
        # is the only actionable state when no row is present.
        st.info(
            "⏳ Semantic model not yet compiled for this table. "
            "Run Source Diagnostic + Compile Semantic Model to populate."
        )
    else:
        alias, ec, pk, nk, joins_json, filters, traps, uses, review, at = sem_rows[0]
        info_cols = st.columns(3)
        info_cols[0].markdown(f"**Canonical alias:** `{alias or '(none)'}`")
        info_cols[1].markdown(f"**Entity class:** `{ec or '(none)'}`")
        info_cols[2].markdown(f"**Review state:** `{review or '(none)'}`")
        st.markdown(f"**Primary key:** `{pk or '(none)'}`")
        st.markdown(f"**Natural key:** `{nk or '(none)'}`")
        if joins_json and joins_json not in ("{}", "null"):
            with st.expander("Typical join keys", expanded=False):
                try:
                    parsed = json.loads(joins_json)
                    st.json(parsed)
                except (json.JSONDecodeError, TypeError):
                    st.code(str(joins_json))
        if filters:
            with st.expander("Typical filters", expanded=False):
                st.markdown(str(filters))
        if traps:
            with st.expander("Common traps", expanded=False):
                st.markdown(str(traps))
        if uses:
            with st.expander("Typical use cases", expanded=False):
                st.markdown(str(uses))
        st.caption(f"Populated at: {at or '?'}")

    st.divider()

    # ── Schema discovery panel ────────────────────────────────────
    st.markdown("### 🔗 Schema Discovery (empirical)")
    sd_rows = query(
        "SELECT id, result_json, executed_at_utc, status "
        "FROM main_seeds.domain_analysis_results "
        "WHERE analysis_type = 'schema_discovery' "
        f"  AND LOWER(source_tables) = '{table.lower()}' "
        "  AND status IN ('success', 'skipped') "
        "ORDER BY executed_at_utc DESC LIMIT 1"
    )
    if sd_rows.empty:
        st.info("⏳ Schema discovery not yet run for this table.")
    else:
        sd = sd_rows.iloc[0]
        age = _format_age(str(sd['executed_at_utc']))
        stale = _is_stale(str(sd['executed_at_utc']))
        stale_tag = (
            " <span style='background:#fbbf24;color:#000;padding:2px 6px;"
            "border-radius:4px;font-size:11px'>stale (>30d)</span>"
            if stale else ""
        )
        st.caption(
            f"Latest DAR: `{sd['id']}` · status: `{sd['status']}` · {age}{stale_tag}",
        )
        if sd['status'] == 'skipped':
            try:
                p = json.loads(sd['result_json']) if sd['result_json'] else {}
                st.warning(f"Skipped — {p.get('skip_reason', '(no reason given)')}")
            except (json.JSONDecodeError, TypeError):
                st.warning("Skipped (malformed result_json).")
        else:
            try:
                payload = json.loads(sd['result_json']) if sd['result_json'] else {}
            except (json.JSONDecodeError, TypeError):
                payload = {}
            pks = payload.get('pk_candidates', [])
            fks = payload.get('fk_candidates', [])
            shapes = payload.get('relationship_shapes', [])
            bridges = payload.get('bridge_tables', [])
            rationale = payload.get('rationale', '')
            if rationale:
                st.markdown(f"_{rationale}_")

            col_pk, col_fk, col_sh, col_br = st.columns(4)
            col_pk.metric("PK candidates", len(pks))
            col_fk.metric("FK candidates", len(fks))
            col_sh.metric("Relationship shapes", len(shapes))
            col_br.metric("Bridges", len(bridges))

            if pks:
                with st.expander("Primary-key candidates", expanded=True):
                    for pk in pks:
                        st.markdown(
                            f"- `{', '.join(pk.get('columns') or [])}` — "
                            f"confidence {pk.get('confidence', '?')}, "
                            f"null_count {pk.get('null_count', '?')}"
                        )
                        st.caption(pk.get('evidence', ''))
            if fks:
                with st.expander("Foreign-key candidates", expanded=True):
                    fk_df = pd.DataFrame([{
                        "from": ",".join(f.get('from_columns') or []),
                        "to_table": f.get('to_table', ''),
                        "to": ",".join(f.get('to_columns') or []),
                        "integrity_pct": f.get('referential_integrity_pct', 0),
                        "confidence": f.get('confidence', ''),
                    } for f in fks])
                    st.dataframe(fk_df, use_container_width=True, hide_index=True)
            if shapes:
                with st.expander("Relationship shapes", expanded=False):
                    for s in shapes:
                        pair = " ↔ ".join(s.get('pair') or [])
                        extra = ""
                        if s.get('sum_match_pct') is not None:
                            extra = f" · sum-match {s['sum_match_pct']}% on {s.get('sum_match_column', '?')}"
                        st.markdown(
                            f"- **{pair}** — {s.get('shape', '?')} "
                            f"({s.get('cardinality', '?')}){extra}"
                        )
                        st.caption(s.get('evidence', ''))
            if bridges:
                with st.expander("Bridge tables (2-hop paths)", expanded=False):
                    for b in bridges:
                        st.markdown(
                            f"- {' → '.join(b.get('between') or [])} via "
                            f"**{b.get('via', '?')}** "
                            f"(conf {b.get('confidence', '?')})"
                        )
                        st.caption(b.get('path', ''))

    st.divider()

    # ── 7-analyzer grid ──────────────────────────────────────────
    st.markdown("### 📊 Analyzer Grid")
    latest = _latest_dars_per_analyzer(table)
    for script_rel, label, _arg in SOURCE_DIAGNOSTIC_ANALYZERS:
        # map registry label to stored analysis_type enum. 'date' in
        # registry maps to 'temporal_coverage' DAR; 'segmentation' maps
        # to 'segmentation_threshold'.
        storage_label = {
            "date": "temporal_coverage",
            "segmentation": "segmentation_threshold",
        }.get(label, label)
        dar = latest.get(storage_label) or latest.get(label)
        if not dar:
            st.markdown(f"- ⏳ **{label}** — no DAR yet")
            continue
        age = _format_age(dar.get('executed_at_utc', ''))
        stale = _is_stale(dar.get('executed_at_utc', ''))
        status = dar.get('status', '?')
        status_icon = {"success": "✅", "skipped": "⏭️", "error": "❌",
                       "superseded": "🔁"}.get(status, "❓")
        stale_tag = " 🟡 stale" if stale else ""
        with st.expander(
            f"{status_icon} **{label}** — `{dar.get('id', '?')}` "
            f"({age}){stale_tag}",
            expanded=False,
        ):
            # Render via Stage D.1 shared renderer
            render_dar_card(dar, st)


def _render_view_c(tables_df: pd.DataFrame) -> None:
    """View C — Catalog Overview + bulk dispatch."""
    st.subheader("📊 Catalog Overview")

    if tables_df.empty:
        st.warning("No tables available.")
        return

    ingested = tables_df[tables_df['ingest_status'] == 'ingested']
    total = len(ingested)
    sem_pop = int(ingested['sem_populated'].sum())
    avg_cov = float(ingested['dar_coverage'].mean()) if total else 0
    n_expected = len(SOURCE_DIAGNOSTIC_ANALYZERS)

    c1, c2, c3 = st.columns(3)
    c1.metric("Ingested raw_sap tables", total)
    c2.metric(
        "Semantic model compiled",
        f"{sem_pop} / {total}",
        delta=f"{(sem_pop/total*100):.0f}%" if total else None,
    )
    c3.metric(
        "Avg DAR coverage",
        f"{avg_cov:.1f} / {n_expected}",
    )

    st.divider()
    st.markdown("### 🔁 Bulk dispatch")

    # Subset picker — defaults to all ingested tables. Analyst narrows
    # to a specific set when doing demo prep (e.g. BG027's 5 scope tables)
    # or resuming partial coverage.
    _default_subset = ingested['table_name'].tolist()
    _picked = st.multiselect(
        "Tables to run (defaults to all ingested)",
        options=_default_subset,
        default=_default_subset,
        key="catalog_bulk_subset",
        help=(
            "Pick a subset to run Source Diagnostic on. "
            "Default is all ingested tables."
        ),
    )
    _n_picked = len(_picked)
    _subprocess_count = _n_picked * n_expected

    st.warning(
        "**Run Source Diagnostic** dispatches "
        f"{n_expected} analyzers × {_n_picked} tables = "
        f"up to {_subprocess_count} subprocess runs, followed by a "
        "bridges-only refresh pass on each table. Expect minutes to "
        "hours depending on data size. Progress shown in status container."
    )
    confirm = st.checkbox(
        "I understand this will take a while and may incur LLM cost (LLM analyzers only)",
        key="catalog_bulk_confirm",
    )
    if st.button(
        f"▶ Run Source Diagnostic ({_n_picked} tables)",
        disabled=(not confirm) or (_n_picked == 0),
        type="primary",
        key="catalog_bulk_run",
    ):
        _run_source_diagnostic_on_all(_picked)
        st.success("Bulk diagnostic complete.")


with tab_diag:
    st.caption(
        "Per-table source characterization: ingested-table inventory, "
        "semantic model compilation status, schema discovery evidence "
        "(PK/FK/shape/bridges), and 7-analyzer grid. Used by Stage A "
        "scope derivation as grounding."
    )

    # Option B routing: radio widget's own key is the single source of
    # truth; button handlers (Detail / Back) write a one-shot
    # `pending_view_change` that the preamble consumes to prime the
    # widget BEFORE the radio instantiates on rerun. Symmetric fix for
    # both button-driven and radio-driven transitions — radio clicks
    # update the widget key directly (Streamlit default behavior) and
    # the preamble leaves them untouched.
    _state_to_name = {
        "tables":       "Ingested Tables",
        "overview":     "Catalog Overview",
        "table_detail": "Table Detail",
    }
    _name_to_state = {v: k for k, v in _state_to_name.items()}

    if 'pending_view_change' in st.session_state:
        _new_view = st.session_state.pop('pending_view_change')
        st.session_state['data_catalog_subtab_radio'] = _state_to_name.get(
            _new_view, "Ingested Tables"
        )

    # First-render default.
    if 'data_catalog_subtab_radio' not in st.session_state:
        st.session_state['data_catalog_subtab_radio'] = "Ingested Tables"

    sub_tabs = st.radio(
        "View",
        ["Ingested Tables", "Catalog Overview", "Table Detail"],
        horizontal=True,
        key='data_catalog_subtab_radio',
    )
    _selected_state = _name_to_state.get(sub_tabs, 'tables')

    _tables_df = _ingested_tables_with_status()

    if _selected_state == 'tables':
        _render_view_a(_tables_df)
    elif _selected_state == 'overview':
        _render_view_c(_tables_df)
    elif _selected_state == 'table_detail':
        ingested_names = _tables_df[
            _tables_df['ingest_status'] == 'ingested'
        ]['table_name'].tolist()
        if not ingested_names:
            st.info("No ingested tables available.")
        else:
            # Render selectbox every rerun; seed index from cached prior
            # selection (from View A Detail click-through or the last
            # widget value). Fallback to 0 if cache is missing or stale
            # (e.g., a previously-ingested table no longer in the list).
            # Cache is then overwritten unconditionally from the widget
            # so widget ↔ cache stay in sync across reruns.
            _cached = st.session_state.get('data_catalog_selected_table')
            _default_idx = resolve_table_detail_default_index(
                _cached, ingested_names
            )
            _selected_table = st.selectbox(
                "Select a table to inspect",
                ingested_names,
                index=_default_idx,
                key='data_catalog_table_selector',
            )
            st.session_state['data_catalog_selected_table'] = _selected_table
            _render_view_b(_selected_table)
