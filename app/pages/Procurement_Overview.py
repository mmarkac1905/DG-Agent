"""Procurement Overview — PO volume, lead times, on-time delivery, cycle times."""
import streamlit as st
import pandas as pd
import plotly.express as px
from db import query
from components.metric_card import metric_with_info


st.title("📦 Procurement Overview")
st.caption("Purchase order analytics · Lead times · On-time delivery · Cycle times")
st.divider()

# --- Inline filters (top of page, not in sidebar) ---
dates = query("SELECT MIN(po_date) as min_d, MAX(po_date) as max_d FROM main_obt.obt_procurement_overview WHERE po_date IS NOT NULL")
min_date = dates['min_d'].iloc[0]
max_date = dates['max_d'].iloc[0]
vendors = query("SELECT DISTINCT vendor_name FROM main_obt.obt_procurement_overview WHERE vendor_name IS NOT NULL ORDER BY vendor_name")
materials = query("SELECT DISTINCT equipment_category FROM main_obt.obt_procurement_overview WHERE equipment_category IS NOT NULL ORDER BY equipment_category")

flt_col1, flt_col2, flt_col3 = st.columns([2, 2, 2])
with flt_col1:
    date_range = st.date_input("PO Date Range", value=(min_date, max_date), min_value=min_date, max_value=max_date)
with flt_col2:
    selected_vendors = st.multiselect("Vendor", vendors['vendor_name'].tolist(), default=[])
with flt_col3:
    selected_materials = st.multiselect("Equipment Category", materials['equipment_category'].tolist(), default=[])

where_parts = ["po_date IS NOT NULL"]
if isinstance(date_range, tuple) and len(date_range) == 2:
    where_parts.append(f"po_date >= '{date_range[0]}' AND po_date <= '{date_range[1]}'")
if selected_vendors:
    vendor_list = "','".join(selected_vendors)
    where_parts.append(f"vendor_name IN ('{vendor_list}')")
if selected_materials:
    mat_list = "','".join(selected_materials)
    where_parts.append(f"equipment_category IN ('{mat_list}')")
where_clause = " AND ".join(where_parts)

df = query(f"SELECT * FROM main_obt.obt_procurement_overview WHERE {where_clause}")

if df.empty:
    st.warning("No data for selected filters.")
    st.stop()

col1, col2, col3, col4, col5 = st.columns(5)
with col1:
    metric_with_info("Total POs", f"{df['purchase_order_number'].nunique():,}", term_id="BG011")
with col2:
    avg_lt = df['lead_time_days'].mean()
    metric_with_info("Avg Lead Time", f"{avg_lt:.1f} days" if pd.notna(avg_lt) else "N/A", term_id="BG001")
with col3:
    otd = df[df['is_on_time'].notna()]['is_on_time'].mean() * 100
    metric_with_info("On-Time Delivery", f"{otd:.1f}%" if pd.notna(otd) else "N/A", term_id="BG002")
with col4:
    avg_cycle = df['po_cycle_days'].mean()
    metric_with_info("Avg PO Cycle", f"{avg_cycle:.1f} days" if pd.notna(avg_cycle) else "N/A", term_id="BG008")
with col5:
    total_value = df['net_value'].sum()
    metric_with_info("Total PO Value", f"€{total_value:,.0f}", term_id="BG012")

st.divider()

chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    st.subheader("PO Volume by Month")
    monthly = df.groupby('po_year_month').agg(
        po_count=('purchase_order_number', 'nunique'),
        total_value=('net_value', 'sum')
    ).reset_index().sort_values('po_year_month')

    fig = px.bar(monthly, x='po_year_month', y='po_count',
                 color_discrete_sequence=['#3b82f6'],
                 labels={'po_year_month': 'Month', 'po_count': 'PO Count'})
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#e0e0e0', xaxis_tickangle=-45, showlegend=False,
        height=380, margin=dict(l=20, r=20, t=30, b=60)
    )
    st.plotly_chart(fig, use_container_width=True)

with chart_col2:
    st.subheader("Lead Time by Vendor")
    vendor_lt = df.groupby('vendor_name').agg(
        avg_lead_time=('lead_time_days', 'mean'),
        n_orders=('purchase_order_number', 'nunique')
    ).reset_index().sort_values('avg_lead_time')

    fig = px.bar(vendor_lt, x='vendor_name', y='avg_lead_time',
                 color_discrete_sequence=['#3b82f6'],
                 labels={'vendor_name': 'Vendor', 'avg_lead_time': 'Avg Lead Time (days)'},
                 hover_data=['n_orders'])
    fig.add_hline(y=45, line_dash="dash", line_color="#8892a4", line_width=1,
                  annotation_text="Avg 45d", annotation_position="top right",
                  annotation_font_color="#8892a4")
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#e0e0e0', showlegend=False,
        height=380, margin=dict(l=20, r=20, t=30, b=80),
    )
    fig.update_xaxes(tickangle=-35)
    fig.update_yaxes(gridcolor='rgba(136, 146, 164, 0.15)')
    st.plotly_chart(fig, use_container_width=True)

chart_col3, chart_col4 = st.columns(2)

with chart_col3:
    st.subheader("On-Time Delivery Rate by Vendor")
    vendor_otd = df[df['is_on_time'].notna()].groupby('vendor_name').agg(
        otd_rate=('is_on_time', 'mean'),
        n_orders=('purchase_order_number', 'nunique')
    ).reset_index()
    vendor_otd['otd_pct'] = vendor_otd['otd_rate'] * 100
    vendor_otd = vendor_otd.sort_values('otd_pct', ascending=False)

    fig = px.bar(vendor_otd, x='vendor_name', y='otd_pct',
                 color_discrete_sequence=['#4ade80'],
                 labels={'vendor_name': 'Vendor', 'otd_pct': 'OTD Rate (%)'},
                 hover_data=['n_orders'])
    fig.add_hline(y=80, line_dash="dash", line_color="#8892a4", line_width=1,
                  annotation_text="Target 80%", annotation_position="top right",
                  annotation_font_color="#8892a4")
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#e0e0e0', showlegend=False,
        height=380, margin=dict(l=20, r=20, t=30, b=80),
        yaxis=dict(range=[0, 100]),
    )
    fig.update_xaxes(tickangle=-35)
    fig.update_yaxes(gridcolor='rgba(136, 146, 164, 0.15)')
    st.plotly_chart(fig, use_container_width=True)

with chart_col4:
    st.subheader("Delivery Status Distribution")
    status_counts = df['delivery_status'].value_counts().reset_index()
    status_counts.columns = ['status', 'count']
    colors = {'fully_received': '#4ade80', 'partially_received': '#fbbf24', 'pending': '#f87171'}

    fig = px.pie(status_counts, values='count', names='status',
                 color='status', color_discrete_map=colors, hole=0.4)
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#e0e0e0', height=380, margin=dict(l=20, r=20, t=30, b=20),
        legend=dict(bgcolor='rgba(0,0,0,0)'),
    )
    st.plotly_chart(fig, use_container_width=True)

st.subheader("Purchase Order Details")
display_cols = [
    'purchase_order_number', 'po_date', 'vendor_name', 'material_number',
    'material_description', 'equipment_category', 'ordered_quantity',
    'unit_price', 'net_value', 'lead_time_days', 'is_on_time',
    'delivery_status', 'plant'
]
existing_cols = [c for c in display_cols if c in df.columns]
_column_labels = {
    'purchase_order_number': 'PO Number',
    'po_date': 'PO Date',
    'vendor_name': 'Vendor',
    'material_number': 'Material',
    'material_description': 'Description',
    'equipment_category': 'Category',
    'ordered_quantity': 'Qty Ordered',
    'unit_price': 'Unit Price',
    'net_value': 'Net Value',
    'lead_time_days': 'Lead Time (d)',
    'is_on_time': 'On Time',
    'delivery_status': 'Delivery Status',
    'plant': 'Plant',
}
_detail = df[existing_cols].sort_values('po_date', ascending=False).rename(columns=_column_labels)
st.dataframe(_detail, use_container_width=True, height=400, hide_index=True)
