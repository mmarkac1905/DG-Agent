"""Inventory Health — stock levels by plant and material, zero-stock alerts."""
import streamlit as st
import pandas as pd
import plotly.express as px
from db import query
from components.metric_card import metric_with_info

st.title("📊 Inventory Health")
st.caption("Stock levels · Zero-stock alerts · Plant distribution · Storage location breakdown")
st.divider()

df = query("SELECT * FROM main_obt.obt_inventory_health")

if df.empty:
    st.warning("No inventory data.")
    st.stop()

col1, col2, col3, col4 = st.columns(4)

total_stock = df['total_stock'].sum()
zero_stock_count = df['is_zero_stock'].sum()
locations_with_stock = len(df[df['total_stock'] > 0])
blocked = df['blocked_stock'].sum()

with col1:
    metric_with_info("Total Stock (units)", f"{total_stock:,.0f}", term_id="BG020")
with col2:
    metric_with_info("Locations with Stock", f"{locations_with_stock}",
                     help_text="Count of material × plant × storage location combinations with total_stock > 0.")
with col3:
    metric_with_info("Zero-Stock Locations", f"{int(zero_stock_count)}",
                     term_id="BG021",
                     delta_color="inverse",
                     delta="Alert" if zero_stock_count > 0 else "OK")
with col4:
    metric_with_info("Blocked Stock", f"{blocked:,.0f}",
                     term_id="BG022",
                     delta="Alert" if blocked > 0 else "OK", delta_color="inverse")

st.divider()

# ============================================================
# Monthly stock flow — derived from goods movements
# ============================================================
# obt_inventory_health is a snapshot (no date column), so we can't show a
# stock-level trend. Goods movements, however, are time-stamped — net flow
# per month tells the business user whether stock is growing or shrinking.
st.subheader("Monthly Stock Flow")
st.caption(
    "Net unit flow per month by movement type — goods receipts add stock, "
    "deployments and vendor returns remove it."
)

flow_df = query(
    """
    SELECT
        DATE_TRUNC('month', posting_date) AS month,
        movement_category,
        SUM(signed_quantity) AS net_units
    FROM main_marts.fact_goods_movements
    WHERE posting_date IS NOT NULL
    GROUP BY 1, 2
    ORDER BY 1, 2
    """
)
if flow_df.empty:
    st.info("No goods movements recorded — stock flow chart unavailable.")
else:
    movement_colors = {
        'goods_receipt':    '#4ade80',
        'deployment':       '#3b82f6',
        'customer_return':  '#fbbf24',
        'vendor_return':    '#f87171',
        'other':            '#6b7280',
    }
    fig = px.bar(
        flow_df, x='month', y='net_units', color='movement_category',
        color_discrete_map=movement_colors, barmode='relative',
        labels={'month': 'Month', 'net_units': 'Net units', 'movement_category': 'Movement'},
    )
    fig.add_hline(y=0, line_width=1, line_color="#4b5563")
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#8892a4', height=380,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    fig.update_xaxes(showgrid=True, gridcolor='#1e2a45')
    fig.update_yaxes(showgrid=True, gridcolor='#1e2a45')
    st.plotly_chart(fig, use_container_width=True)

    # Cumulative net stock — a proxy for stock level over time
    cum_df = (
        flow_df.groupby('month')['net_units'].sum().cumsum().reset_index()
    )
    if not cum_df.empty:
        st.markdown("**Cumulative Net Units (proxy for stock level)**")
        fig2 = px.area(
            cum_df, x='month', y='net_units',
            color_discrete_sequence=['#3b82f6'],
            labels={'month': 'Month', 'net_units': 'Cumulative net units'},
        )
        fig2.update_layout(
            plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
            font_color='#8892a4', height=280,
        )
        fig2.update_xaxes(showgrid=True, gridcolor='#1e2a45')
        fig2.update_yaxes(showgrid=True, gridcolor='#1e2a45')
        st.plotly_chart(fig2, use_container_width=True)

st.divider()

chart_col1, chart_col2 = st.columns(2)

with chart_col1:
    st.subheader("Stock by Plant")
    plant_stock = df.groupby(['plant_code', 'plant_name']).agg(
        unrestricted=('unrestricted_stock', 'sum'),
        blocked=('blocked_stock', 'sum'),
        qi=('quality_inspection_stock', 'sum'),
    ).reset_index()

    fig = px.bar(plant_stock, x='plant_name',
                 y=['unrestricted', 'blocked', 'qi'],
                 barmode='stack',
                 color_discrete_sequence=['#4ade80', '#f87171', '#fbbf24'],
                 labels={'value': 'Units', 'plant_name': 'Plant'})
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#8892a4', height=400, legend_title="Stock Type"
    )
    st.plotly_chart(fig, use_container_width=True)

with chart_col2:
    st.subheader("Stock by Equipment Category")
    cat_stock = df.groupby('equipment_category').agg(
        total=('total_stock', 'sum')
    ).reset_index().sort_values('total', ascending=False)

    fig = px.bar(cat_stock, x='equipment_category', y='total',
                 color_discrete_sequence=['#3b82f6'],
                 labels={'equipment_category': 'Category', 'total': 'Units'})
    fig.update_layout(
        plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
        font_color='#8892a4', height=400, showlegend=False
    )
    st.plotly_chart(fig, use_container_width=True)

st.subheader("Stock by Storage Location Type")
loc_stock = df.groupby(['location_type', 'storage_location_name']).agg(
    total=('total_stock', 'sum')
).reset_index()

fig = px.treemap(loc_stock, path=['location_type', 'storage_location_name'], values='total',
                 color='total', color_continuous_scale='Blues')
fig.update_layout(
    plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
    font_color='#8892a4', height=400
)
st.plotly_chart(fig, use_container_width=True)

st.subheader("Stock Detail")
display_df = df[['material_number', 'material_description', 'equipment_category',
                  'plant_code', 'plant_name', 'storage_location', 'storage_location_name',
                  'unrestricted_stock', 'blocked_stock', 'quality_inspection_stock', 'total_stock']].copy()
display_df = display_df.sort_values(['plant_code', 'material_number']).rename(columns={
    'material_number': 'Material',
    'material_description': 'Description',
    'equipment_category': 'Category',
    'plant_code': 'Plant',
    'plant_name': 'Plant Name',
    'storage_location': 'Storage Loc',
    'storage_location_name': 'Storage Loc Name',
    'unrestricted_stock': 'Unrestricted',
    'blocked_stock': 'Blocked',
    'quality_inspection_stock': 'QI Stock',
    'total_stock': 'Total',
})
st.dataframe(display_df, use_container_width=True, hide_index=True, height=400)
