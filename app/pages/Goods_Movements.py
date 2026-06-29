"""Goods Movements — movement type analysis, inbound vs outbound trends."""
import streamlit as st
import pandas as pd
import plotly.express as px
from db import query
from components.metric_card import metric_with_info

st.title("🔄 Goods Movements")
st.caption("Movement analysis · Inbound vs outbound · Deployment tracking · Return trends")
st.divider()

# --- Inline filters (top of page, not in sidebar) ---
categories = query("SELECT DISTINCT movement_category FROM main_obt.obt_goods_movements WHERE movement_category IS NOT NULL ORDER BY 1")
flt_col1, flt_col2 = st.columns([2, 3])
with flt_col1:
    directions = st.multiselect("Direction", ['inbound', 'outbound', 'transfer'], default=['inbound', 'outbound', 'transfer'])
with flt_col2:
    selected_cats = st.multiselect("Movement Category", categories['movement_category'].tolist(), default=[])

where_parts = ["posting_date IS NOT NULL"]
if directions:
    dir_list = "','".join(directions)
    where_parts.append(f"movement_direction IN ('{dir_list}')")
if selected_cats:
    cat_list = "','".join(selected_cats)
    where_parts.append(f"movement_category IN ('{cat_list}')")
where_clause = " AND ".join(where_parts)

df = query(f"SELECT * FROM main_obt.obt_goods_movements WHERE {where_clause}")

if df.empty:
    st.warning("No movement data for selected filters.")
    st.stop()

col1, col2, col3, col4, col5 = st.columns(5)

total_movements = len(df)
inbound = len(df[df['movement_direction'] == 'inbound'])
outbound = len(df[df['movement_direction'] == 'outbound'])
deployments = len(df[df['movement_category'] == 'deployment'])
returns = len(df[df['movement_category'].isin(['customer_return', 'vendor_return'])])

with col1:
    metric_with_info("Total Movements", f"{total_movements:,}", term_id="BG023")
with col2:
    metric_with_info("Inbound", f"{inbound:,}",
                     help_text="Movement types 101 (GR), 161 (customer return), 202 (GI reversal), 561 (initial stock).")
with col3:
    metric_with_info("Outbound", f"{outbound:,}",
                     help_text="Movement types 102 (GR reversal), 122 (vendor return), 201 (deployment).")
with col4:
    metric_with_info("Deployments", f"{deployments:,}", term_id="BG018",
                     help_text="Movement type 201 — CPE issued to technician for customer installation.")
with col5:
    metric_with_info("Returns", f"{returns:,}", term_id="BG019",
                     help_text="Movement types 161 (customer return) + 122 (vendor return).")

st.divider()

colors = {
    'goods_receipt': '#4ade80', 'deployment': '#3b82f6',
    'customer_return': '#fbbf24', 'vendor_return': '#f87171',
    'gr_reversal': '#a78bfa', 'gi_reversal': '#c084fc',
    'plant_transfer': '#06b6d4', 'initial_stock': '#6b7280'
}

chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    st.subheader("Movements by Category")
    cat_counts = df['movement_category'].value_counts().reset_index()
    cat_counts.columns = ['category', 'count']
    fig = px.pie(cat_counts, values='count', names='category',
                 color='category', color_discrete_map=colors, hole=0.4)
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#8892a4', height=400
    )
    st.plotly_chart(fig, use_container_width=True)

with chart_col2:
    st.subheader("Monthly Movement Trend")
    if 'posting_year_month' in df.columns:
        monthly = df.groupby(['posting_year_month', 'movement_direction']).size().reset_index(name='count')
        monthly = monthly.sort_values('posting_year_month')
        fig = px.line(monthly, x='posting_year_month', y='count', color='movement_direction',
                      markers=True, color_discrete_map={
                          'inbound': '#4ade80', 'outbound': '#f87171', 'transfer': '#06b6d4'
                      })
        fig.update_layout(
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font_color='#8892a4', height=400, xaxis_tickangle=-45
        )
        st.plotly_chart(fig, use_container_width=True)

st.subheader("Movements by Equipment Category")
mat_movements = df.groupby(['equipment_category', 'movement_category']).agg(
    total_qty=('quantity', 'sum'),
    count=('material_document_number', 'count')
).reset_index()

fig = px.bar(mat_movements, x='equipment_category', y='count', color='movement_category',
             color_discrete_map=colors, barmode='stack',
             labels={'equipment_category': 'Category', 'count': 'Movement Count'})
fig.update_layout(
    plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
    font_color='#8892a4', height=400
)
st.plotly_chart(fig, use_container_width=True)

st.subheader("Deployment vs Return Ratio by Material")
deploy_return = df[df['movement_category'].isin(['deployment', 'customer_return'])].groupby(
    ['equipment_category', 'movement_category']
).agg(total_qty=('quantity', 'sum')).reset_index()
deploy_return_pivot = deploy_return.pivot(index='equipment_category', columns='movement_category', values='total_qty').fillna(0)
if 'deployment' in deploy_return_pivot.columns and 'customer_return' in deploy_return_pivot.columns:
    deploy_return_pivot['return_rate'] = (deploy_return_pivot['customer_return'] / deploy_return_pivot['deployment'].replace(0, pd.NA) * 100).round(2)
    st.dataframe(deploy_return_pivot.reset_index(), use_container_width=True, hide_index=True)

st.subheader("Recent Movements")
recent = df.sort_values('posting_date', ascending=False).head(50)
display_cols = ['posting_date', 'material_document_number', 'movement_type',
                'movement_description', 'movement_direction', 'material_number',
                'equipment_category', 'quantity', 'plant', 'posted_by']
existing_cols = [c for c in display_cols if c in recent.columns]
_labels = {
    'posting_date': 'Posted',
    'material_document_number': 'Doc #',
    'movement_type': 'Mvt Type',
    'movement_description': 'Description',
    'movement_direction': 'Direction',
    'material_number': 'Material',
    'equipment_category': 'Category',
    'quantity': 'Qty',
    'plant': 'Plant',
    'posted_by': 'Posted By',
}
st.dataframe(recent[existing_cols].rename(columns=_labels),
             use_container_width=True, hide_index=True, height=400)
