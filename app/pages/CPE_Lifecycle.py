"""CPE Lifecycle — device status distribution, deployment rates, defect tracking."""
import streamlit as st
import pandas as pd
import plotly.express as px
from db import query
from components.metric_card import metric_with_info

st.title("📱 CPE Lifecycle")
st.caption("Device status · Deployment rates · Defect tracking · Serial-level traceability")
st.divider()

# --- Inline filters (matches Procurement Overview pattern) ---
dates = query(
    "SELECT MIN(status_from_date) AS min_d, MAX(status_from_date) AS max_d "
    "FROM main_marts.fact_equipment_lifecycle WHERE status_from_date IS NOT NULL"
)
min_date = dates['min_d'].iloc[0]
max_date = dates['max_d'].iloc[0]

categories = query(
    "SELECT DISTINCT equipment_category FROM main_obt.obt_cpe_lifecycle "
    "WHERE equipment_category IS NOT NULL ORDER BY equipment_category"
)

flt_col1, flt_col2 = st.columns([2, 2])
with flt_col1:
    date_range = st.date_input(
        "Status Change Date Range",
        value=(min_date, max_date),
        min_value=min_date, max_value=max_date,
    )
with flt_col2:
    selected_categories = st.multiselect(
        "Equipment Category",
        categories['equipment_category'].tolist(),
        default=[],
    )

# --- Current-state OBT (used for KPIs + snapshot charts below) ---
obt_where = ["lifecycle_status IS NOT NULL"]
if selected_categories:
    cat_list = "','".join(selected_categories)
    obt_where.append(f"equipment_category IN ('{cat_list}')")
obt_where_clause = " AND ".join(obt_where)

df = query(f"SELECT * FROM main_obt.obt_cpe_lifecycle WHERE {obt_where_clause}")

if df.empty:
    st.warning("No CPE lifecycle data for the current filters.")
    st.stop()

# --- Detect whether the user narrowed the date window (or just opened it full) ---
def _to_date(v):
    if hasattr(v, 'to_pydatetime'):
        return v.to_pydatetime().date()
    if hasattr(v, 'date') and callable(getattr(v, 'date', None)):
        try:
            return v.date()
        except Exception:
            return v
    return v

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = _to_date(date_range[0]), _to_date(date_range[1])
else:
    start_date, end_date = _to_date(min_date), _to_date(max_date)

date_filtered = not (start_date == _to_date(min_date) and end_date == _to_date(max_date))

col1, col2, col3, col4, col5 = st.columns(5)

if not date_filtered:
    # --- Unfiltered: current-state snapshot from the OBT view ---
    total = len(df)
    deployed = len(df[df['lifecycle_status'] == 'deployed'])
    in_stock = len(df[df['lifecycle_status'] == 'in_stock'])
    returned = len(df[df['lifecycle_status'] == 'returned'])
    defective = len(df[df['lifecycle_status'] == 'defective'])

    with col1:
        metric_with_info("Total CPE Devices", f"{total:,}", term_id="BG017")
    with col2:
        metric_with_info("Deployed", f"{deployed:,}", term_id="BG018",
                         delta=f"{deployed/total*100:.1f}%")
    with col3:
        metric_with_info("In Stock", f"{in_stock:,}", term_id="BG006",
                         delta=f"{in_stock/total*100:.1f}%")
    with col4:
        metric_with_info("Returned", f"{returned:,}", term_id="BG019",
                         delta=f"{returned/total*100:.1f}%", delta_color="inverse")
    with col5:
        metric_with_info("Defective", f"{defective:,}", term_id="BG003",
                         delta=f"{defective/total*100:.1f}%", delta_color="inverse")
    st.caption("Showing current state across the full history. Pick a date range above to see activity for that period.")
else:
    # --- Date-filtered: count activity (transitions) inside the selected window ---
    # Category scope (same subquery we use for the trend charts)
    cat_scope = ""
    if selected_categories:
        cat_list = "','".join(selected_categories)
        cat_scope = (
            " AND equipment_number IN (SELECT equipment_number "
            f"FROM main_obt.obt_cpe_lifecycle WHERE equipment_category IN ('{cat_list}'))"
        )

    activity = query(
        f"""
        SELECT
            SUM(CASE WHEN status_sequence = 1 THEN 1 ELSE 0 END) AS received,
            SUM(CASE WHEN lifecycle_status = 'deployed'  THEN 1 ELSE 0 END) AS deployed,
            SUM(CASE WHEN lifecycle_status = 'returned'  THEN 1 ELSE 0 END) AS returned_qty,
            SUM(CASE WHEN lifecycle_status = 'defective' THEN 1 ELSE 0 END) AS defective
        FROM main_marts.fact_equipment_lifecycle
        WHERE status_from_date >= '{start_date}'
          AND status_from_date <= '{end_date}'
          {cat_scope}
        """
    )

    received  = int(activity['received'].iloc[0] or 0)
    deployed  = int(activity['deployed'].iloc[0] or 0)
    returned  = int(activity['returned_qty'].iloc[0] or 0)
    defective = int(activity['defective'].iloc[0] or 0)
    # Net stock movement in the window — received adds to stock, deployed + defective remove it
    net_change = received - deployed - defective

    def _pct(n):
        return f"{(n / received * 100):.1f}% of received" if received else ""

    with col1:
        metric_with_info(
            "Received",
            f"{received:,}",
            help_text="Devices whose first lifecycle status began inside the selected date range.",
        )
    with col2:
        metric_with_info(
            "Deployed",
            f"{deployed:,}",
            term_id="BG018",
            delta=_pct(deployed),
        )
    with col3:
        metric_with_info(
            "Returned",
            f"{returned:,}",
            term_id="BG019",
            delta=_pct(returned),
            delta_color="inverse",
        )
    with col4:
        metric_with_info(
            "Defective",
            f"{defective:,}",
            term_id="BG003",
            delta=_pct(defective),
            delta_color="inverse",
        )
    with col5:
        metric_with_info(
            "Net Change",
            f"{net_change:+,}",
            help_text=(
                "Net stock delta for the period: Received − Deployed − Defective. "
                "Positive = stock grew; negative = stock shrank."
            ),
            delta="growing" if net_change > 0 else ("shrinking" if net_change < 0 else "flat"),
            delta_color="normal" if net_change > 0 else ("inverse" if net_change < 0 else "off"),
        )
    st.caption(
        f"Showing activity between **{start_date}** and **{end_date}**. "
        "Clear the date range to see the current-state totals."
    )

st.divider()

colors = {
    'deployed': '#4ade80', 'in_stock': '#3b82f6',
    'returned': '#fbbf24', 'defective': '#f87171', 'unknown': '#6b7280',
}

# --- Build the WHERE clause for the SCD2 fact ---
fact_where = ["status_from_date IS NOT NULL"]
if isinstance(date_range, tuple) and len(date_range) == 2:
    fact_where.append(
        f"status_from_date >= '{date_range[0]}' AND status_from_date <= '{date_range[1]}'"
    )
if selected_categories:
    cat_list = "','".join(selected_categories)
    # fact_equipment_lifecycle doesn't carry equipment_category directly — join via material
    fact_where.append(
        "equipment_number IN (SELECT equipment_number FROM main_obt.obt_cpe_lifecycle "
        f"WHERE equipment_category IN ('{cat_list}'))"
    )
fact_where_clause = " AND ".join(fact_where)

# ============================================================
# Time-based analysis — trend + time-to-deploy
# ============================================================
st.subheader("Lifecycle Trends Over Time")

trend_col1, trend_col2 = st.columns(2)

with trend_col1:
    st.markdown("**Monthly Status Transitions**")
    st.caption("How many devices entered each lifecycle status per month")
    trend_df = query(
        f"""
        SELECT
            DATE_TRUNC('month', status_from_date) AS month,
            lifecycle_status,
            COUNT(*) AS device_count
        FROM main_marts.fact_equipment_lifecycle
        WHERE {fact_where_clause}
        GROUP BY 1, 2
        ORDER BY 1, 2
        """
    )
    if trend_df.empty:
        st.info("No status transitions in the selected range.")
    else:
        fig = px.line(
            trend_df, x="month", y="device_count", color="lifecycle_status",
            color_discrete_map=colors, markers=True,
            labels={"month": "Month", "device_count": "Devices", "lifecycle_status": "Status"},
        )
        fig.update_layout(
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font_color='#8892a4', height=380,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        fig.update_xaxes(showgrid=True, gridcolor='#1e2a45')
        fig.update_yaxes(showgrid=True, gridcolor='#1e2a45')
        st.plotly_chart(fig, use_container_width=True)

with trend_col2:
    st.markdown("**Average Days from Receipt to Deployment**")
    st.caption("How long devices sit in stock before a technician picks them up")
    tod_df = query(
        f"""
        SELECT
            DATE_TRUNC('month', status_from_date) AS deployment_month,
            ROUND(AVG(days_receipt_to_deployment), 1) AS avg_days_to_deploy,
            COUNT(*) AS deployments
        FROM main_marts.fact_equipment_lifecycle
        WHERE {fact_where_clause}
          AND lifecycle_status = 'deployed'
          AND days_receipt_to_deployment IS NOT NULL
        GROUP BY 1
        ORDER BY 1
        """
    )
    if tod_df.empty:
        st.info("No deployments in the selected range.")
    else:
        fig = px.bar(
            tod_df, x="deployment_month", y="avg_days_to_deploy",
            color_discrete_sequence=['#3b82f6'],
            labels={"deployment_month": "Month", "avg_days_to_deploy": "Avg days in stock"},
            hover_data={"deployments": True},
        )
        fig.update_layout(
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font_color='#8892a4', height=380,
        )
        fig.update_xaxes(showgrid=True, gridcolor='#1e2a45')
        fig.update_yaxes(showgrid=True, gridcolor='#1e2a45')
        st.plotly_chart(fig, use_container_width=True)

# --- Rolling defect rate (shows whether quality is trending up or down) ---
defect_trend = query(
    f"""
    WITH monthly AS (
        SELECT
            DATE_TRUNC('month', status_from_date) AS month,
            COUNT(*) FILTER (WHERE lifecycle_status = 'defective') AS defective_count,
            COUNT(*) AS total_transitions
        FROM main_marts.fact_equipment_lifecycle
        WHERE {fact_where_clause}
        GROUP BY 1
    )
    SELECT
        month,
        defective_count,
        total_transitions,
        ROUND(100.0 * defective_count / NULLIF(total_transitions, 0), 2) AS defect_rate_pct
    FROM monthly
    ORDER BY month
    """
)
if not defect_trend.empty and defect_trend['defect_rate_pct'].notna().any():
    st.markdown("**Monthly Defect Rate Trend**")
    st.caption("Share of status transitions that were flagged defective — PR005 threshold = 5%")
    fig = px.line(
        defect_trend, x="month", y="defect_rate_pct", markers=True,
        color_discrete_sequence=['#f87171'],
        labels={"month": "Month", "defect_rate_pct": "Defect rate (%)"},
    )
    fig.add_hline(y=5, line_dash="dash", line_color="#f87171",
                  annotation_text="5% Threshold (PR005)", annotation_position="top left")
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#8892a4', height=300,
    )
    fig.update_xaxes(showgrid=True, gridcolor='#1e2a45')
    fig.update_yaxes(showgrid=True, gridcolor='#1e2a45')
    st.plotly_chart(fig, use_container_width=True)

st.divider()

# ============================================================
# Snapshot charts (current state — unaffected by the date filter)
# ============================================================
st.subheader("Current State Snapshot")
st.caption("These charts reflect the current status of every device, not the filtered date range.")

chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    st.markdown("**Lifecycle Status Distribution**")
    status_counts = df['lifecycle_status'].value_counts().reset_index()
    status_counts.columns = ['status', 'count']
    fig = px.pie(status_counts, values='count', names='status',
                 color='status', color_discrete_map=colors, hole=0.4)
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#8892a4', height=400,
    )
    st.plotly_chart(fig, use_container_width=True)

with chart_col2:
    st.markdown("**Devices by Equipment Category**")
    cat_status = df.groupby(['equipment_category', 'lifecycle_status']).size().reset_index(name='count')
    fig = px.bar(cat_status, x='equipment_category', y='count', color='lifecycle_status',
                 color_discrete_map=colors, barmode='stack',
                 labels={'equipment_category': 'Category', 'count': 'Devices'})
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#8892a4', height=400,
    )
    st.plotly_chart(fig, use_container_width=True)

st.markdown("**Defect Rate by Equipment Category**")
defect_analysis = df.groupby('equipment_category').agg(
    total=('equipment_number', 'count'),
    defective=('lifecycle_status', lambda x: (x == 'defective').sum()),
    returned=('lifecycle_status', lambda x: (x == 'returned').sum()),
).reset_index()
defect_analysis['defect_rate'] = (defect_analysis['defective'] / defect_analysis['total'] * 100).round(2)
defect_analysis['return_rate'] = (defect_analysis['returned'] / defect_analysis['total'] * 100).round(2)

fig = px.bar(defect_analysis, x='equipment_category', y=['defect_rate', 'return_rate'],
             barmode='group', color_discrete_sequence=['#f87171', '#fbbf24'],
             labels={'value': 'Rate (%)', 'equipment_category': 'Category'})
fig.add_hline(y=5, line_dash="dash", line_color="#f87171", annotation_text="5% Threshold (PR005)")
fig.update_layout(
    plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
    font_color='#8892a4', height=350,
)
st.plotly_chart(fig, use_container_width=True)

st.markdown("**Devices by Manufacturer**")
mfr = df.groupby(['manufacturer', 'lifecycle_status']).size().reset_index(name='count')
fig = px.bar(mfr, x='manufacturer', y='count', color='lifecycle_status',
             color_discrete_map=colors, barmode='stack',
             labels={'manufacturer': 'Manufacturer', 'count': 'Devices'})
fig.update_layout(
    plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
    font_color='#8892a4', height=350,
)
st.plotly_chart(fig, use_container_width=True)

st.subheader("Device Lookup")
search = st.text_input("Search by serial number or equipment number")
if search:
    results = df[
        df['serial_number'].str.contains(search, case=False, na=False) |
        df['equipment_number'].str.contains(search, case=False, na=False)
    ]
    if results.empty:
        st.info("No devices found.")
    else:
        st.dataframe(results, use_container_width=True, hide_index=True, height=300)
