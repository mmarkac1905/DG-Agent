"""Vendor Scorecard — performance ranking, concentration risk, OTD trends."""
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.subplots as sp
from db import query
from components.metric_card import metric_with_info

st.title("🏢 Vendor Scorecard")
st.caption("Vendor performance · Concentration risk · On-time delivery trends")
st.divider()

df = query("SELECT * FROM main_obt.obt_vendor_scorecard ORDER BY year, quarter")

if df.empty:
    st.warning("No vendor scorecard data.")
    st.stop()

st.subheader("Vendor Performance Ranking (All Time)")

vendor_summary = df.groupby(['vendor_name', 'vendor_region']).agg(
    total_spend=('total_spend', 'sum'),
    avg_lead_time=('avg_lead_time_days', 'mean'),
    avg_otd=('on_time_delivery_rate', 'mean'),
    total_orders=('total_po_items', 'sum'),
    avg_concentration=('vendor_spend_share', 'mean'),
).reset_index()
vendor_summary['avg_otd_pct'] = vendor_summary['avg_otd'] * 100
vendor_summary['avg_concentration_pct'] = vendor_summary['avg_concentration'] * 100
vendor_summary = vendor_summary.sort_values('total_spend', ascending=False)

col1, col2, col3 = st.columns(3)
with col1:
    metric_with_info("Active Vendors", f"{vendor_summary['vendor_name'].nunique()}",
                     help_text="Count of distinct vendors with at least one PO.")
with col2:
    total_spend = vendor_summary['total_spend'].sum()
    metric_with_info("Total Procurement Spend", f"€{total_spend:,.0f}", term_id="BG012")
with col3:
    max_conc = vendor_summary['avg_concentration_pct'].max()
    vendor_max = vendor_summary.loc[vendor_summary['avg_concentration_pct'].idxmax(), 'vendor_name']
    metric_with_info(
        "Highest Concentration",
        f"{max_conc:.1f}% ({vendor_max})",
        term_id="BG010",
        delta="RISK" if max_conc > 60 else "OK",
        delta_color="inverse",
    )

st.divider()

fig = px.scatter(vendor_summary, x='avg_lead_time', y='avg_otd_pct',
                 size='total_spend', color='vendor_name',
                 hover_data=['total_orders', 'avg_concentration_pct', 'vendor_region'],
                 labels={
                     'avg_lead_time': 'Avg Lead Time (days)',
                     'avg_otd_pct': 'On-Time Delivery (%)',
                     'total_spend': 'Total Spend'
                 },
                 title="Vendor Performance Matrix (size = spend)")
fig.add_hline(y=80, line_dash="dash", line_color="#f87171", annotation_text="OTD Target")
fig.update_layout(
    plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
    font_color='#8892a4', height=450, margin=dict(l=20, r=20, t=50, b=20)
)
st.plotly_chart(fig, use_container_width=True)

st.subheader("Vendor Concentration Risk by Quarter")
st.caption("Procurement rule PR004: No vendor should exceed 60% of total spend")

concentration = df[df['vendor_spend_share'] > 0.01].copy()
concentration['spend_pct'] = concentration['vendor_spend_share'] * 100

fig = px.bar(concentration, x='year_quarter', y='spend_pct', color='vendor_name',
             labels={'year_quarter': 'Quarter', 'spend_pct': 'Spend Share (%)'},
             barmode='stack')
fig.add_hline(y=60, line_dash="dash", line_color="#f87171", annotation_text="60% Threshold")
fig.update_layout(
    plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)',
    font_color='#8892a4', height=400, margin=dict(l=20, r=20, t=30, b=60)
)
st.plotly_chart(fig, use_container_width=True)

risky = df[df['concentration_risk_flag'] == True]
if not risky.empty:
    st.error(f"⚠️ Concentration risk detected in {risky['year_quarter'].nunique()} quarters — "
             f"{risky['vendor_name'].iloc[0]} exceeds 60% threshold")

st.subheader("On-Time Delivery Performance")
st.caption("Target: **80%** (red dashed line)")

otd_trend = df[['vendor_name', 'year_quarter', 'on_time_delivery_rate']].copy()
otd_trend['otd_pct'] = (otd_trend['on_time_delivery_rate'] * 100).round(1)

# Deterministic vendor order — by highest lifetime spend first, so the
# most important vendors land in the top-left cells of each layout.
vendor_order = (
    df.groupby('vendor_name')['total_spend'].sum()
    .sort_values(ascending=False)
    .index.tolist()
)
quarter_order = sorted(otd_trend['year_quarter'].dropna().unique().tolist())

# --- OPTION A: Small multiples / faceted trend chart ---
# One mini-chart per vendor, line coloured by the vendor's LATEST OTD
# against the 80% target, so at-a-glance you see who's red/amber/green.
n_vendors = len(vendor_order)
n_cols = 4
n_rows = max(1, (n_vendors + n_cols - 1) // n_cols)

facet_fig = sp.make_subplots(
    rows=n_rows,
    cols=n_cols,
    subplot_titles=[v[:22] for v in vendor_order],
    shared_yaxes=True,
    shared_xaxes=True,
    vertical_spacing=0.18,
    horizontal_spacing=0.06,
)

for i, vendor in enumerate(vendor_order):
    row = i // n_cols + 1
    col = i % n_cols + 1
    vdata = (
        otd_trend[otd_trend['vendor_name'] == vendor]
        .sort_values('year_quarter')
    )
    if vdata.empty:
        continue

    latest_otd = float(vdata['otd_pct'].iloc[-1])
    if latest_otd >= 80:
        line_color = '#4ade80'
    elif latest_otd >= 60:
        line_color = '#fbbf24'
    else:
        line_color = '#f87171'

    facet_fig.add_trace(
        go.Scatter(
            x=vdata['year_quarter'],
            y=vdata['otd_pct'],
            mode='lines+markers',
            line=dict(color=line_color, width=2),
            marker=dict(size=5, color=line_color),
            name=vendor,
            hovertemplate=f"<b>{vendor}</b><br>%{{x}}<br>OTD: %{{y:.1f}}%<extra></extra>",
            showlegend=False,
        ),
        row=row, col=col,
    )
    facet_fig.add_hline(
        y=80, line_color='#f87171', line_dash='dash', line_width=1,
        row=row, col=col,
    )

# Make the subplot titles readable in dark mode and shrink axis labels
for ann in facet_fig.layout.annotations:
    ann.font = dict(size=10, color='#e0e0e0')

facet_fig.update_yaxes(
    range=[0, 100],
    showgrid=True, gridcolor='#1e2a45',
    tickfont=dict(size=9, color='#8892a4'),
    zeroline=False,
)
facet_fig.update_xaxes(
    showgrid=False,
    tickfont=dict(size=8, color='#8892a4'),
    tickangle=-45,
)
facet_fig.update_layout(
    height=420,
    paper_bgcolor='rgba(0,0,0,0)',
    plot_bgcolor='rgba(0,0,0,0)',
    font_color='#8892a4',
    margin=dict(l=40, r=10, t=40, b=50),
)

# --- OPTION B: Heatmap — rows=vendors, cols=quarters, colour=OTD% ---
pivot_df = (
    otd_trend.pivot_table(
        index='vendor_name', columns='year_quarter',
        values='otd_pct', aggfunc='mean',
    )
    .reindex(index=vendor_order, columns=quarter_order)
)

# Text labels inside each cell (integer percent, or empty for NaN cells)
text_vals = pivot_df.round(0).astype('Int64').astype(str).replace('<NA>', '').values
text_vals_with_pct = [
    [(v + '%') if v else '' for v in row] for row in text_vals
]

heatmap_fig = go.Figure(
    data=go.Heatmap(
        z=pivot_df.values,
        x=list(pivot_df.columns),
        y=list(pivot_df.index),
        zmin=0, zmax=100,
        colorscale=[
            [0.0,  '#7f1d1d'],   # deep red = severe miss
            [0.5,  '#f87171'],   # red
            [0.65, '#fbbf24'],   # amber
            [0.80, '#4ade80'],   # green = hits target
            [1.0,  '#16a34a'],   # deep green = excellent
        ],
        text=text_vals_with_pct,
        texttemplate='%{text}',
        textfont=dict(size=10, color='#0a0e17'),
        hovertemplate='<b>%{y}</b><br>%{x}<br>OTD: %{z:.1f}%<extra></extra>',
        colorbar=dict(
            title=dict(text='OTD %', font=dict(color='#8892a4')),
            tickfont=dict(color='#8892a4'),
            thickness=12,
        ),
    )
)
heatmap_fig.update_layout(
    height=420,
    paper_bgcolor='rgba(0,0,0,0)',
    plot_bgcolor='rgba(0,0,0,0)',
    font_color='#8892a4',
    margin=dict(l=10, r=10, t=30, b=50),
    xaxis=dict(tickfont=dict(size=10), tickangle=-45),
    yaxis=dict(tickfont=dict(size=10), automargin=True),
)

otd_col1, otd_col2 = st.columns(2)
with otd_col1:
    st.markdown("**Per-vendor trend (small multiples)**")
    st.plotly_chart(facet_fig, use_container_width=True)
with otd_col2:
    st.markdown("**Heatmap — vendor × quarter**")
    st.plotly_chart(heatmap_fig, use_container_width=True)

st.subheader("Vendor Detail Table")
display_df = vendor_summary[['vendor_name', 'vendor_region', 'total_orders', 'total_spend',
                              'avg_lead_time', 'avg_otd_pct', 'avg_concentration_pct']].copy()
display_df.columns = ['Vendor', 'Region', 'Total Orders', 'Total Spend (€)',
                       'Avg Lead Time (d)', 'OTD Rate (%)', 'Avg Spend Share (%)']
display_df['Total Spend (€)'] = display_df['Total Spend (€)'].apply(lambda x: f"€{x:,.0f}")
display_df['Avg Lead Time (d)'] = display_df['Avg Lead Time (d)'].apply(lambda x: f"{x:.1f}")
display_df['OTD Rate (%)'] = display_df['OTD Rate (%)'].apply(lambda x: f"{x:.1f}%")
display_df['Avg Spend Share (%)'] = display_df['Avg Spend Share (%)'].apply(lambda x: f"{x:.1f}%")

st.dataframe(display_df, use_container_width=True, hide_index=True)
