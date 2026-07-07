"""Data Model — interactive ERD per architecture layer.

Pick a layer (SAP Source / Data Vault / Data Marts / OBT) and see:
  - All tables in that layer as draggable boxes, color-coded by entity type
  - Named relationships drawn as lines with labels and cardinality
  - Key columns marked with 🔑 inside each box
  - A detail panel below the diagram with the column-level view
"""
import json
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

from db import query, get_connection
from vault_docs import describe_vault_column

st.title("🗂️ Data Model")
st.caption("Entity-Relationship diagrams per architecture layer")
st.divider()

DB_PATH = Path(__file__).resolve().parent.parent.parent / "cpe_analytics.duckdb"

import os as _os
_active_src = _os.environ.get("DG_SOURCE_SCHEMA", "raw_sap")
_src_label = f"Source ({_active_src})"

selected_layer = st.selectbox(
    "Select Layer",
    [
        _src_label,
        "Data Vault (main_vault)",
        "Data Marts (main_marts)",
        "OBT Views (main_obt)",
    ],
    key="dm_layer",
)

SCHEMA_MAP = {
    _src_label:                _active_src,
    "Data Vault (main_vault)": "main_vault",
    "Data Marts (main_marts)": "main_marts",
    "OBT Views (main_obt)":    "main_obt",
}
schema = SCHEMA_MAP[selected_layer]

if _active_src != "raw_sap" and schema == _active_src:
    st.caption(
        "This source's model is drawn from **measured evidence**: every edge "
        "below was empirically tested by the join-cardinality analyzer "
        "(1:1 / 1:N verified; ⚠ marks directions that multiply rows)."
    )


# ------------------------------------------------------------------
# Domain groupings — which tables belong to each business-process lens
# ------------------------------------------------------------------

DOMAIN_GROUPS = {
    "raw_sap": {
        "All tables":                  None,  # no filter
        "Procurement":                 ["ekko", "ekpo", "eket", "ekkn", "eban", "ebkn"],
        "Goods Receipt":               ["mkpf", "mseg", "ekbe"],
        "Vendor Master":               ["lfa1", "lfb1", "lfm1"],
        "Material Master":             ["mara", "makt", "marc", "marm"],
        "Equipment & Serial":          ["equi", "eqbs", "objk", "seri", "ser01", "ser03"],
        "Inventory":                   ["mard"],
        "Invoice":                     ["rbkp", "rseg"],
        "Accounting":                  ["bkpf", "bseg"],
        "Org Structure":               ["t001", "t001w", "t001l", "t023", "t024", "t024e", "t156"],
        "Procure-to-Pay (full flow)":  ["ekko", "ekpo", "eket", "mkpf", "mseg", "rbkp", "rseg", "lfa1", "mara"],
    },
    "main_vault": {
        "All entities":          None,
        "Purchase Order domain": [
            "hub_purchase_order", "link_po_vendor", "link_po_material", "link_po_plant",
            "sat_po_header", "sat_po_item", "sat_po_schedule", "sat_po_account",
        ],
        "Vendor domain": [
            "hub_vendor", "sat_vendor_general", "sat_vendor_commercial",
        ],
        "Material domain": [
            "hub_material", "sat_material_general", "sat_material_description", "sat_material_plant",
        ],
        "Equipment domain": [
            "hub_equipment", "link_equipment_material", "link_equipment_gr",
            "sat_equipment_general", "sat_equipment_status",
        ],
        "Goods Receipt domain": [
            "hub_material_document", "link_gr_po", "link_gr_material", "sat_gr_header", "sat_gr_item",
        ],
    },
    "main_marts": {
        "All models":     None,
        "Procurement":    ["fact_purchase_orders", "dim_vendor", "dim_material", "dim_date", "dim_plant"],
        "Equipment":      ["fact_equipment_lifecycle", "dim_equipment", "dim_material"],
        "Inventory":      ["fact_inventory", "dim_material", "dim_plant", "dim_storage_location"],
        "Goods Movements":["fact_goods_movements", "dim_material", "dim_plant", "dim_movement_type", "dim_date"],
    },
    "main_obt": {
        "All views": None,
    },
}

# Explicit per-table positions for each domain. Each coordinate is the
# top-left of a 220x220 entity box. Tuned so the central entity sits
# near canvas (400-500, 260-300) and related tables hug it.
DOMAIN_LAYOUTS = {
    "raw_sap": {
        "Procurement": {
            "ekko":  (400, 260),
            "ekpo":  (400, 520),
            "eket":  (680, 520),
            "ekkn":  (680, 260),
            "eban":  (100,  80),
            "ebkn":  (100, 340),
        },
        "Goods Receipt": {
            "mkpf": (400, 260),
            "mseg": (400, 520),
            "ekbe": (720, 260),
        },
        "Vendor Master": {
            "lfa1": (400, 260),
            "lfb1": (700,  80),
            "lfm1": (700, 440),
        },
        "Material Master": {
            "mara": (400, 260),
            "makt": ( 80, 260),
            "marc": (700,  80),
            "marm": (700, 440),
        },
        "Equipment & Serial": {
            "equi":  (400, 300),
            "eqbs":  (400,  40),
            "objk":  ( 80, 300),
            "seri":  (720, 300),
            "ser01": (720, 560),
            "ser03": ( 80, 560),
        },
        "Inventory": {
            "mard": (400, 260),
        },
        "Invoice": {
            "rbkp": (250, 260),
            "rseg": (620, 260),
        },
        "Accounting": {
            "bkpf": (250, 260),
            "bseg": (620, 260),
        },
        "Org Structure": {
            "t001":  (400, 300),
            "t001w": (250,  80),
            "t001l": (550,  80),
            "t023":  ( 80, 300),
            "t024":  (720, 300),
            "t024e": (250, 520),
            "t156":  (550, 520),
        },
        "Procure-to-Pay (full flow)": {
            "lfa1": (100, 100),
            "ekko": (380, 100),
            "ekpo": (380, 340),
            "eket": (380, 580),
            "mkpf": (660, 100),
            "mseg": (660, 340),
            "rbkp": (940, 100),
            "rseg": (940, 340),
            "mara": (100, 580),
        },
    },
    "main_vault": {
        "Purchase Order domain": {
            "hub_purchase_order": (420, 300),
            "link_po_vendor":     (100,  80),
            "link_po_material":   (100, 320),
            "link_po_plant":      (100, 560),
            "sat_po_header":      (760,  80),
            "sat_po_item":        (760, 260),
            "sat_po_schedule":    (760, 440),
            "sat_po_account":     (760, 620),
        },
        "Vendor domain": {
            "hub_vendor":           (400, 300),
            "sat_vendor_general":   (720, 120),
            "sat_vendor_commercial":(720, 480),
        },
        "Material domain": {
            "hub_material":             (420, 300),
            "sat_material_general":     ( 80, 120),
            "sat_material_description": ( 80, 480),
            "sat_material_plant":       (760, 300),
        },
        "Equipment domain": {
            "hub_equipment":          (420, 300),
            "link_equipment_material":(420,  60),
            "link_equipment_gr":      (420, 560),
            "sat_equipment_general":  ( 80, 300),
            "sat_equipment_status":   (760, 300),
        },
        "Goods Receipt domain": {
            "hub_material_document": (420, 300),
            "link_gr_po":            ( 80, 100),
            "link_gr_material":      (760, 100),
            "sat_gr_header":         ( 80, 500),
            "sat_gr_item":           (760, 500),
        },
    },
    "main_marts": {
        "Procurement": {
            "fact_purchase_orders": (420, 300),
            "dim_vendor":           (420,  60),
            "dim_material":         ( 80, 300),
            "dim_date":             (760, 300),
            "dim_plant":            (420, 560),
        },
        "Equipment": {
            "fact_equipment_lifecycle": (420, 300),
            "dim_equipment":            ( 80, 300),
            "dim_material":             (760, 300),
        },
        "Inventory": {
            "fact_inventory":        (420, 300),
            "dim_material":          (420,  60),
            "dim_plant":             ( 80, 300),
            "dim_storage_location":  (760, 300),
        },
        "Goods Movements": {
            "fact_goods_movements": (420, 300),
            "dim_material":         (140,  80),
            "dim_plant":            (700,  80),
            "dim_movement_type":    (140, 540),
            "dim_date":             (700, 540),
        },
    },
    "main_obt": {},
}

_all_groups = DOMAIN_GROUPS.get(schema, {})
# Hide the "All tables / All entities / All models" catch-all for layers
# that have any real process/domain split — the full-layer view is too
# dense to be useful. Fall back to the original options for layers that
# have nothing but the catch-all (e.g. OBT with its five views).
_filtered = [k for k in _all_groups.keys() if not k.lower().startswith("all ")]
domain_options = _filtered if _filtered else list(_all_groups.keys())
if not domain_options:
    domain_options = ["No processes found"]
domain_label_map = {
    "raw_sap":    "Filter by Process",
    "main_vault": "Filter by Domain",
    "main_marts": "Filter by Domain",
    "main_obt":   "Filter",
}
selected_domain = st.selectbox(
    domain_label_map.get(schema, "Filter by Domain"),
    domain_options,
    index=0,
    key="dm_domain",
)

conn = get_connection()  # cached in-memory Parquet-backed
tables_df = conn.execute(
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema = ? ORDER BY table_name",
    [schema],
).fetchdf()
columns_df = conn.execute(
    "SELECT table_name, column_name, data_type, ordinal_position "
    "FROM information_schema.columns WHERE table_schema = ? "
    "ORDER BY table_name, ordinal_position",
    [schema],
).fetchdf()

# In a non-default source, the vault/mart/obt schemas also hold the SAP
# demo's models; show only the active source's generated models there.
# Source-isolated models live under dbt/models/<src>/ and scan into the
# model catalog as layer='other' (mart names don't carry the source
# token, so a name filter alone misses them).
if _active_src != "raw_sap" and schema in ("main_vault", "main_marts", "main_obt"):
    _tok = _active_src[4:] if _active_src.startswith("raw_") else _active_src
    try:
        _src_models = set(
            conn.execute(
                "SELECT LOWER(model_name) AS m FROM main_seeds.dbt_model_catalog "
                "WHERE layer = 'other'"
            ).fetchdf()["m"]
        )
    except Exception:
        _src_models = set()
    tables_df = tables_df[
        tables_df["table_name"].str.lower().isin(_src_models)
        | tables_df["table_name"].str.contains(_tok, case=False)
    ].reset_index(drop=True)

# Apply the domain filter to the full table list
domain_filter = DOMAIN_GROUPS.get(schema, {}).get(selected_domain)
if domain_filter is not None:
    filter_set = set(domain_filter)
    tables_df = tables_df[tables_df["table_name"].isin(filter_set)].reset_index(drop=True)
    columns_df = columns_df[columns_df["table_name"].isin(filter_set)].reset_index(drop=True)

# Explicit per-table positions for the active domain (if any)
preset_positions = DOMAIN_LAYOUTS.get(schema, {}).get(selected_domain, {})


def _classify(schema_name: str, table_name: str) -> str:
    name = table_name.lower()
    if schema_name == "main_vault":
        if name.startswith("hub_"):
            return "hub"
        if name.startswith("link_"):
            return "link"
        if name.startswith("sat_"):
            return "satellite"
        return "other"
    if schema_name == "main_marts":
        if name.startswith("dim_"):
            return "dimension"
        if name.startswith("fact_"):
            return "fact"
        return "other"
    if schema_name == "main_obt":
        return "obt"
    return "source"


def _is_key(col_name: str) -> bool:
    n = str(col_name or '').lower()
    return (
        n.startswith("hk_")
        or n.endswith("_number")
        or n.endswith("_id")
        or n.endswith("_code")
        or n == "hashdiff"
    )


# Build entity payload for the diagram
entities = []
for t in tables_df["table_name"].tolist():
    tcols = columns_df[columns_df["table_name"] == t]
    cols_payload = [
        {
            "name": row["column_name"],
            "type": str(row["data_type"]),
            "is_key": _is_key(row["column_name"]),
        }
        for _, row in tcols.iterrows()
    ]
    preset = preset_positions.get(t)
    entity = {
        "name": t,
        "type": _classify(schema, t),
        "columns": cols_payload[:15],
        "total_columns": len(cols_payload),
    }
    if preset is not None:
        entity["preset_x"] = preset[0]
        entity["preset_y"] = preset[1]
    entities.append(entity)


# ------------------------------------------------------------------
# Hard-coded relationships per layer (named + cardinality)
# ------------------------------------------------------------------

SAP_RELATIONSHIPS = [
    {"from": "ekko", "to": "ekpo", "name": "contains",       "label": "PO has line items",              "cardinality": "1:N"},
    {"from": "ekko", "to": "eket", "name": "scheduled_as",   "label": "PO has delivery schedule",       "cardinality": "1:N"},
    {"from": "ekko", "to": "ekkn", "name": "charged_to",     "label": "PO has cost assignment",         "cardinality": "1:N"},
    {"from": "ekko", "to": "lfa1", "name": "ordered_from",   "label": "PO placed with vendor",          "cardinality": "N:1"},
    {"from": "ekpo", "to": "mara", "name": "orders",         "label": "Item orders material",           "cardinality": "N:1"},
    {"from": "ekpo", "to": "t001w","name": "delivered_to",   "label": "Item delivered to plant",        "cardinality": "N:1"},
    {"from": "mseg", "to": "mkpf", "name": "part_of",        "label": "Movement in document",           "cardinality": "N:1"},
    {"from": "mseg", "to": "ekko", "name": "fulfills",       "label": "GR fulfills PO",                 "cardinality": "N:1"},
    {"from": "mseg", "to": "mara", "name": "moves",          "label": "Movement of material",           "cardinality": "N:1"},
    {"from": "ekbe", "to": "ekko", "name": "history_of",     "label": "History entry for PO",           "cardinality": "N:1"},
    {"from": "rseg", "to": "rbkp", "name": "line_of",        "label": "Invoice line item",              "cardinality": "N:1"},
    {"from": "rbkp", "to": "ekko", "name": "invoices",       "label": "Invoice for PO",                 "cardinality": "N:1"},
    {"from": "equi", "to": "mara", "name": "instance_of",    "label": "Device is type of material",     "cardinality": "N:1"},
    {"from": "eqbs", "to": "equi", "name": "status_of",      "label": "Status history for device",      "cardinality": "N:1"},
    {"from": "objk", "to": "equi", "name": "links_serial",   "label": "Serial linked to equipment",     "cardinality": "1:1"},
    {"from": "seri", "to": "mkpf", "name": "assigned_at",    "label": "Serial assigned at GR",          "cardinality": "N:1"},
    {"from": "mard", "to": "mara", "name": "stock_of",       "label": "Stock level for material",       "cardinality": "N:1"},
    {"from": "mard", "to": "t001w","name": "stored_at",      "label": "Stock at plant",                 "cardinality": "N:1"},
    {"from": "marc", "to": "mara", "name": "plant_settings", "label": "Material settings per plant",    "cardinality": "N:1"},
    {"from": "eban", "to": "ekko", "name": "converted_to",   "label": "PR converted to PO",             "cardinality": "N:1"},
    {"from": "lfa1", "to": "lfb1", "name": "has_financials", "label": "Vendor financial data",          "cardinality": "1:1"},
    {"from": "lfa1", "to": "lfm1", "name": "has_purchasing", "label": "Vendor purchasing data",         "cardinality": "1:1"},
    {"from": "bseg", "to": "bkpf", "name": "line_of",        "label": "FI line item for accounting doc","cardinality": "N:1"},
    {"from": "rbkp", "to": "bkpf", "name": "posted_as",      "label": "Invoice posts an accounting doc","cardinality": "1:1"},
]

VAULT_RELATIONSHIPS = [
    # Links → hubs
    {"from": "link_po_vendor",          "to": "hub_purchase_order",       "name": "connects", "label": "PO-to-vendor link",             "cardinality": "N:1"},
    {"from": "link_po_vendor",          "to": "hub_vendor",               "name": "connects", "label": "PO-to-vendor link",             "cardinality": "N:1"},
    {"from": "link_po_material",        "to": "hub_purchase_order",       "name": "connects", "label": "PO item orders material",       "cardinality": "N:1"},
    {"from": "link_po_material",        "to": "hub_material",             "name": "connects", "label": "PO item orders material",       "cardinality": "N:1"},
    {"from": "link_po_plant",           "to": "hub_purchase_order",       "name": "connects", "label": "PO delivered to plant",         "cardinality": "N:1"},
    {"from": "link_po_plant",           "to": "hub_plant",                "name": "connects", "label": "PO delivered to plant",         "cardinality": "N:1"},
    {"from": "link_gr_po",              "to": "hub_material_document",    "name": "connects", "label": "GR fulfills PO",                "cardinality": "N:1"},
    {"from": "link_gr_po",              "to": "hub_purchase_order",       "name": "connects", "label": "GR fulfills PO",                "cardinality": "N:1"},
    {"from": "link_gr_material",        "to": "hub_material_document",    "name": "connects", "label": "GR receives material",          "cardinality": "N:1"},
    {"from": "link_gr_material",        "to": "hub_material",             "name": "connects", "label": "GR receives material",          "cardinality": "N:1"},
    {"from": "link_equipment_material", "to": "hub_equipment",            "name": "connects", "label": "Device is type of material",    "cardinality": "N:1"},
    {"from": "link_equipment_material", "to": "hub_material",             "name": "connects", "label": "Device is type of material",    "cardinality": "N:1"},
    {"from": "link_equipment_gr",       "to": "hub_equipment",            "name": "connects", "label": "Device received in GR",         "cardinality": "N:1"},
    {"from": "link_equipment_gr",       "to": "hub_material_document",    "name": "connects", "label": "Device received in GR",         "cardinality": "N:1"},
    {"from": "link_invoice_po",         "to": "hub_invoice",              "name": "connects", "label": "Invoice for PO",                "cardinality": "N:1"},
    {"from": "link_invoice_po",         "to": "hub_purchase_order",       "name": "connects", "label": "Invoice for PO",                "cardinality": "N:1"},
    {"from": "link_pr_po",              "to": "hub_purchase_requisition", "name": "connects", "label": "PR converted to PO",            "cardinality": "N:1"},
    {"from": "link_pr_po",              "to": "hub_purchase_order",       "name": "connects", "label": "PR converted to PO",            "cardinality": "N:1"},

    # Satellites → hubs
    {"from": "sat_vendor_general",      "to": "hub_vendor",               "name": "describes", "label": "Name, country, address",       "cardinality": "N:1"},
    {"from": "sat_vendor_commercial",   "to": "hub_vendor",               "name": "describes", "label": "Payment terms, recon account", "cardinality": "N:1"},
    {"from": "sat_material_general",    "to": "hub_material",             "name": "describes", "label": "Type, group, weight",          "cardinality": "N:1"},
    {"from": "sat_material_description","to": "hub_material",             "name": "describes", "label": "Product description text",     "cardinality": "N:1"},
    {"from": "sat_material_plant",      "to": "hub_material",             "name": "describes", "label": "Material settings per plant",  "cardinality": "N:1"},
    {"from": "sat_stock_level",         "to": "hub_material",             "name": "describes", "label": "Current stock quantities",     "cardinality": "N:1"},
    {"from": "sat_po_header",           "to": "hub_purchase_order",       "name": "describes", "label": "Date, status, value",          "cardinality": "N:1"},
    {"from": "sat_po_item",             "to": "hub_purchase_order",       "name": "describes", "label": "Line item details",            "cardinality": "N:1"},
    {"from": "sat_po_schedule",         "to": "hub_purchase_order",       "name": "describes", "label": "Delivery schedule lines",      "cardinality": "N:1"},
    {"from": "sat_po_account",          "to": "hub_purchase_order",       "name": "describes", "label": "Cost center / GL account",     "cardinality": "N:1"},
    {"from": "sat_equipment_general",   "to": "hub_equipment",            "name": "describes", "label": "Serial, manufacturer, model",  "cardinality": "N:1"},
    {"from": "sat_equipment_status",    "to": "hub_equipment",            "name": "tracks",    "label": "Lifecycle status (SCD2)",      "cardinality": "N:1"},
    {"from": "sat_gr_header",           "to": "hub_material_document",    "name": "describes", "label": "Posting date, user, reference","cardinality": "N:1"},
    {"from": "sat_gr_item",             "to": "hub_material_document",    "name": "describes", "label": "GR line item quantities",      "cardinality": "N:1"},
    {"from": "sat_invoice_header",      "to": "hub_invoice",              "name": "describes", "label": "Invoice date, amount, ref",    "cardinality": "N:1"},
    {"from": "sat_pr_detail",           "to": "hub_purchase_requisition", "name": "describes", "label": "Material, qty, status",        "cardinality": "N:1"},
]

MART_RELATIONSHIPS = [
    {"from": "fact_purchase_orders",    "to": "dim_vendor",           "name": "ordered_from",  "label": "PO placed with vendor",       "cardinality": "N:1"},
    {"from": "fact_purchase_orders",    "to": "dim_material",         "name": "orders",        "label": "PO orders material type",     "cardinality": "N:1"},
    {"from": "fact_purchase_orders",    "to": "dim_plant",            "name": "delivered_to",  "label": "PO item delivery plant",      "cardinality": "N:1"},
    {"from": "fact_purchase_orders",    "to": "dim_storage_location", "name": "stored_in",     "label": "PO item storage location",    "cardinality": "N:1"},
    {"from": "fact_purchase_orders",    "to": "dim_date",             "name": "created_on",    "label": "PO creation date",            "cardinality": "N:1"},
    {"from": "fact_goods_movements",    "to": "dim_material",         "name": "moves",         "label": "Movement involves material",  "cardinality": "N:1"},
    {"from": "fact_goods_movements",    "to": "dim_plant",            "name": "at",            "label": "Movement at plant",           "cardinality": "N:1"},
    {"from": "fact_goods_movements",    "to": "dim_storage_location", "name": "in",            "label": "Movement in storage location","cardinality": "N:1"},
    {"from": "fact_goods_movements",    "to": "dim_movement_type",    "name": "classified_as", "label": "Movement type category",      "cardinality": "N:1"},
    {"from": "fact_goods_movements",    "to": "dim_vendor",           "name": "from_vendor",   "label": "GR received from vendor",     "cardinality": "N:1"},
    {"from": "fact_goods_movements",    "to": "dim_date",             "name": "posted_on",     "label": "Posting date",                "cardinality": "N:1"},
    {"from": "fact_equipment_lifecycle","to": "dim_equipment",        "name": "tracks",        "label": "Lifecycle event for device",  "cardinality": "N:1"},
    {"from": "fact_equipment_lifecycle","to": "dim_material",         "name": "device_type",   "label": "Device is a material type",   "cardinality": "N:1"},
    {"from": "fact_equipment_lifecycle","to": "dim_plant",            "name": "at",            "label": "Device currently at plant",   "cardinality": "N:1"},
    {"from": "fact_equipment_lifecycle","to": "dim_date",             "name": "event_on",      "label": "Lifecycle event date",        "cardinality": "N:1"},
    {"from": "fact_inventory",          "to": "dim_material",         "name": "stock_of",      "label": "Stock for material type",     "cardinality": "N:1"},
    {"from": "fact_inventory",          "to": "dim_plant",            "name": "at",            "label": "Stock at warehouse",          "cardinality": "N:1"},
    {"from": "fact_inventory",          "to": "dim_storage_location", "name": "in",            "label": "Stock in storage location",   "cardinality": "N:1"},
    {"from": "fact_invoices",           "to": "dim_vendor",           "name": "from",          "label": "Invoice from vendor",         "cardinality": "N:1"},
    {"from": "fact_invoices",           "to": "dim_date",             "name": "issued_on",     "label": "Invoice date",                "cardinality": "N:1"},
    {"from": "dim_equipment",           "to": "dim_material",         "name": "type_of",       "label": "Device is type of material",  "cardinality": "N:1"},
]

OBT_RELATIONSHIPS = [
    {"from": "obt_procurement_overview", "to": "obt_vendor_scorecard", "name": "feeds",       "label": "PO data feeds vendor metrics", "cardinality": ""},
    {"from": "obt_cpe_lifecycle",        "to": "obt_goods_movements",  "name": "related",     "label": "Device movements tracked",     "cardinality": ""},
    {"from": "obt_inventory_health",     "to": "obt_goods_movements",  "name": "impacted_by", "label": "Stock changed by movements",   "cardinality": ""},
]

RELATIONSHIPS_BY_SCHEMA = {
    "raw_sap":     SAP_RELATIONSHIPS,
    "main_vault":  VAULT_RELATIONSHIPS,
    "main_marts":  MART_RELATIONSHIPS,
    "main_obt":    OBT_RELATIONSHIPS,
}
relationships = list(RELATIONSHIPS_BY_SCHEMA.get(schema, []))


def _measured_source_relationships(src_tables: set) -> list:
    """Edges for a non-SAP source layer, from the empirically measured
    join-cardinality evidence (latest non-superseded DAR per pair+key).
    Nothing is hand-curated: what the analyzers measured is what renders."""
    try:
        df = conn.execute(
            "SELECT id, result_json FROM main_seeds.domain_analysis_results "
            "WHERE analysis_type = 'join_cardinality' AND status = 'success' "
            "AND (superseded_by IS NULL OR superseded_by = '') "
            "ORDER BY executed_at_utc"
        ).fetchdf()
    except Exception:
        return []
    out: dict = {}
    for _, r in df.iterrows():
        try:
            j = json.loads(r["result_json"])
        except Exception:
            continue
        t1 = (j.get("t1") or "").lower()
        t2 = (j.get("t2") or "").lower()
        if t1 not in src_tables or t2 not in src_tables:
            continue
        cls = j.get("fanout_class")
        if cls == "no_signal":
            continue
        keys = "+".join(j.get("key_columns_t1") or [])
        avg = j.get("avg_fanout")
        if cls == "per_record_key":
            card = "1:1"
        elif cls == "header_detail":
            card = f"1:N (avg {avg}x)" if avg is not None else "1:N"
        else:
            card = f"⚠ x{avg:.0f} fanout" if avg is not None else "⚠ fanout"
        out[(t1, t2, keys)] = {
            "from": t1, "to": t2, "name": "measured",
            "label": f"on {keys}", "cardinality": card,
        }
    return list(out.values())


if _active_src != "raw_sap" and schema == _active_src:
    relationships = _measured_source_relationships(
        {x.lower() for x in tables_df["table_name"].tolist()}
    )

# Layer in extractor-derived edges from dbt_model_relationships seed.
# scripts/extract_dbt_relationships.py parses ref() calls and walks
# column lineage to raw SAP fields, so fact↔dim joins the hand-curated
# list forgot (e.g. fact_purchase_orders → dim_plant via WERKS) are
# picked up automatically. The seed is the source of truth; the curated
# MART_RELATIONSHIPS list supplements it with edges the extractor can't
# derive (e.g. fact_equipment_lifecycle.plant → dim_plant, which uses
# SAP field GEWRK instead of WERKS).
try:
    _rel_seed = conn.execute(
        "SELECT from_model, to_model, relationship_type, join_key, label "
        "FROM main_seeds.dbt_model_relationships "
        "WHERE from_layer = 'marts' AND to_layer = 'marts'"
    ).fetchdf()
except Exception:
    _rel_seed = None

if schema == "main_marts" and _rel_seed is not None and not _rel_seed.empty:
    existing = {(r["from"], r["to"]) for r in relationships}
    _label_by_type = {"star": "Star join", "date": "Calendar join", "pipeline": "Reads from"}
    for _, r in _rel_seed.iterrows():
        key = (r["from_model"], r["to_model"])
        if key in existing:
            continue
        relationships.append({
            "from":        r["from_model"],
            "to":          r["to_model"],
            "name":        r["relationship_type"] or "related",
            "label":       r["label"] or _label_by_type.get(r["relationship_type"], "join"),
            "cardinality": "N:1",
        })
        existing.add(key)

# Drop any relationships that reference tables not in this schema (guard
# against renamed or dropped models)
known_names = set(tables_df["table_name"].tolist())
relationships = [
    r for r in relationships if r["from"] in known_names and r["to"] in known_names
]

TYPE_COLORS = {
    "hub":       {"bg": "#1e3a5f", "border": "#3b82f6", "label": "HUB"},
    "link":      {"bg": "#2e1065", "border": "#a78bfa", "label": "LINK"},
    "satellite": {"bg": "#3b1050", "border": "#c084fc", "label": "SAT"},
    "dimension": {"bg": "#14532d", "border": "#4ade80", "label": "DIM"},
    "fact":      {"bg": "#422006", "border": "#fbbf24", "label": "FACT"},
    "obt":       {"bg": "#422006", "border": "#fb923c", "label": "OBT"},
    "source":    {"bg": "#1f2937", "border": "#6b7280", "label": "SRC"},
    "other":     {"bg": "#1f2937", "border": "#6b7280", "label": ""},
}

entities_json = json.dumps(entities)
relationships_json = json.dumps(relationships)
type_colors_json = json.dumps(TYPE_COLORS)

# ------------------------------------------------------------------
# Diagram HTML
# ------------------------------------------------------------------
html = f"""
<style>
    html, body {{ margin: 0; padding: 0; background: transparent; }}
    #erd-wrap {{
        position: relative;
        width: 100%;
        height: 720px;
        background: #0a0e17;
        border: 1px solid #1e2a45;
        border-radius: 8px;
        overflow: auto;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    }}
    #erd-inner {{
        position: relative;
        transform-origin: 0 0;
    }}
    .erd-controls {{
        position: sticky;
        top: 10px;
        left: 10px;
        z-index: 20;
        display: inline-flex;
        gap: 6px;
        margin: 10px;
    }}
    .erd-btn {{
        background: #131a2e;
        border: 1px solid #1e2a45;
        color: #8892a4;
        padding: 4px 12px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 12px;
        font-family: inherit;
    }}
    .erd-btn:hover {{
        border-color: #3b82f6;
        color: #e0e0e0;
    }}
    .entity-box {{
        position: absolute;
        background: #131a2e;
        border: 2px solid #1e2a45;
        border-radius: 8px;
        min-width: 190px;
        max-width: 220px;
        cursor: grab;
        box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        transition: box-shadow 0.15s;
        z-index: 2;
    }}
    .entity-box:hover {{
        box-shadow: 0 4px 16px rgba(59, 130, 246, 0.25);
    }}
    .entity-header {{
        padding: 6px 10px;
        border-bottom: 1px solid #1e2a45;
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 6px;
    }}
    .entity-name {{
        color: #e0e0e0;
        font-size: 11px;
        font-weight: 700;
        font-family: 'SF Mono', Consolas, monospace;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
    .entity-badge {{
        font-size: 8px;
        font-weight: 700;
        padding: 1px 5px;
        border-radius: 3px;
        letter-spacing: 0.5px;
        flex-shrink: 0;
    }}
    .entity-columns {{
        padding: 4px 10px;
        max-height: 200px;
        overflow-y: auto;
    }}
    .entity-col {{
        display: flex;
        justify-content: space-between;
        padding: 1px 0;
        font-size: 10px;
        color: #8892a4;
        gap: 6px;
    }}
    .entity-col.key {{ color: #60a5fa; font-weight: 600; }}
    .entity-col .col-type {{
        color: #4b5563;
        font-size: 9px;
        text-transform: lowercase;
        white-space: nowrap;
    }}
    .entity-col-count {{
        padding: 2px 10px 4px 10px;
        font-size: 9px;
        color: #4b5563;
        border-top: 1px solid #1e2a45;
        font-style: italic;
    }}
    .erd-line {{
        position: absolute;
        height: 0;
        border-top: 2px solid #3b82f6;
        transform-origin: 0 0;
        pointer-events: none;
        z-index: 50;
    }}
    .erd-line-label {{
        position: absolute;
        background: #0a0e17;
        padding: 2px 6px;
        border-radius: 3px;
        font-size: 10px;
        color: #8892a4;
        text-align: center;
        white-space: nowrap;
        pointer-events: none;
        z-index: 51;
        line-height: 1.25;
        border: 1px solid #1e2a45;
    }}
    .erd-line-label strong {{ color: #e0e0e0; }}
    .erd-line-label .card {{ color: #6b7280; font-size: 9px; }}
</style>

<div id="erd-wrap">
    <div class="erd-controls">
        <button class="erd-btn" onclick="window.erdZoom(1.2)">+</button>
        <button class="erd-btn" onclick="window.erdZoom(0.8)">−</button>
        <button class="erd-btn" onclick="window.erdReset()">Reset</button>
    </div>
    <div id="erd-inner">
        <div id="erd-lines"></div>
    </div>
</div>

<script>
(() => {{
    const entities = {entities_json};
    const relationships = {relationships_json};
    const typeColors = {type_colors_json};

    const wrap = document.getElementById('erd-wrap');
    const inner = document.getElementById('erd-inner');
    const linesLayer = document.getElementById('erd-lines');
    let scale = 1;

    const positions = {{}};
    const elements = {{}};
    const BOX_W = 220;
    const BOX_H_EST = 220;
    const GAP_X = 30;
    const GAP_Y = 30;
    const COLS_PER_ROW = 4;

    // If every entity has a preset position (domain-layout mode), use those directly.
    // Otherwise fall back to the type-grouped auto-grid used for the "All tables" view.
    const hasPresets = entities.length > 0 && entities.every(e =>
        typeof e.preset_x === 'number' && typeof e.preset_y === 'number'
    );

    const layoutGroups = {{}};
    const typeOrder = ['hub', 'link', 'satellite', 'fact', 'dimension', 'obt', 'source', 'other'];
    if (!hasPresets) {{
        entities.forEach(e => {{
            if (!layoutGroups[e.type]) layoutGroups[e.type] = [];
            layoutGroups[e.type].push(e);
        }});
    }}

    function placeEntity(entity, x, y) {{
        positions[entity.name] = {{ x, y }};

        const colors = typeColors[entity.type] || typeColors['other'];
        const box = document.createElement('div');
        box.className = 'entity-box';
        box.style.left = x + 'px';
        box.style.top = y + 'px';
        box.style.borderColor = colors.border;

        const colsHtml = entity.columns.map(c => {{
            const keyClass = c.is_key ? 'key' : '';
            const icon = c.is_key ? '🔑 ' : '';
            return '<div class="entity-col ' + keyClass + '">' +
                   '<span>' + icon + c.name + '</span>' +
                   '<span class="col-type">' + c.type + '</span>' +
                   '</div>';
        }}).join('');

        const moreHtml = entity.total_columns > 15
            ? '<div class="entity-col-count">+ ' + (entity.total_columns - 15) + ' more columns</div>'
            : '';

        box.innerHTML =
            '<div class="entity-header" style="background:' + colors.bg + '">' +
                '<span class="entity-name">' + entity.name + '</span>' +
                '<span class="entity-badge" style="background:' + colors.border + ';color:#fff">' + colors.label + '</span>' +
            '</div>' +
            '<div class="entity-columns">' + colsHtml + '</div>' +
            moreHtml;

        let dragging = false, sx = 0, sy = 0, bx = 0, by = 0;
        box.addEventListener('mousedown', (e) => {{
            dragging = true;
            sx = e.clientX; sy = e.clientY;
            bx = parseFloat(box.style.left); by = parseFloat(box.style.top);
            box.style.cursor = 'grabbing';
            box.style.zIndex = 100;
            e.preventDefault();
        }});
        document.addEventListener('mousemove', (e) => {{
            if (!dragging) return;
            const nx = bx + (e.clientX - sx) / scale;
            const ny = by + (e.clientY - sy) / scale;
            box.style.left = nx + 'px';
            box.style.top = ny + 'px';
            positions[entity.name] = {{ x: nx, y: ny }};
            drawLines();
        }});
        document.addEventListener('mouseup', () => {{
            if (dragging) {{
                dragging = false;
                box.style.cursor = 'grab';
                box.style.zIndex = 2;
            }}
        }});

        inner.appendChild(box);
        elements[entity.name] = box;
    }}

    if (hasPresets) {{
        entities.forEach(e => placeEntity(e, e.preset_x, e.preset_y));
    }} else {{
        let currentY = 60;
        typeOrder.forEach(type => {{
            const group = layoutGroups[type];
            if (!group || !group.length) return;
            const perRow = (type === 'satellite' || type === 'link') ? 5 : COLS_PER_ROW;
            group.forEach((entity, i) => {{
                const row = Math.floor(i / perRow);
                const col = i % perRow;
                const x = 20 + col * (BOX_W + GAP_X);
                const y = currentY + row * (BOX_H_EST + GAP_Y);
                placeEntity(entity, x, y);
            }});
            const rows = Math.ceil(group.length / perRow);
            currentY += rows * (BOX_H_EST + GAP_Y) + 30;
        }});
    }}

    // Resize the inner container so all entities fit
    function fitCanvas() {{
        let maxX = 0, maxY = 0;
        Object.values(positions).forEach(p => {{
            if (p.x + BOX_W > maxX) maxX = p.x + BOX_W;
            if (p.y + BOX_H_EST > maxY) maxY = p.y + BOX_H_EST;
        }});
        inner.style.width = (maxX + 40) + 'px';
        inner.style.height = (maxY + 40) + 'px';
    }}

    // Return the point on rect(cx,cy,w,h) where a line toward (tx,ty) exits.
    // Used so lines connect to the nearest edge instead of overlapping the box body.
    function edgePoint(cx, cy, w, h, tx, ty) {{
        const dx = tx - cx;
        const dy = ty - cy;
        if (dx === 0 && dy === 0) return {{ x: cx, y: cy }};
        const hw = w / 2;
        const hh = h / 2;
        const adx = Math.abs(dx);
        const ady = Math.abs(dy);
        // Pick the axis whose edge is hit first
        if (adx * hh >= ady * hw) {{
            // Exits through left or right edge
            const sign = dx >= 0 ? 1 : -1;
            return {{ x: cx + sign * hw, y: cy + (dy * hw) / Math.max(adx, 0.001) }};
        }} else {{
            // Exits through top or bottom edge
            const sign = dy >= 0 ? 1 : -1;
            return {{ x: cx + (dx * hh) / Math.max(ady, 0.001), y: cy + sign * hh }};
        }}
    }}

    function drawLines() {{
        fitCanvas();
        // Clear previous frame
        linesLayer.innerHTML = '';

        relationships.forEach(rel => {{
            const fromBox = elements[rel.from];
            const toBox = elements[rel.to];
            const fromPos = positions[rel.from];
            const toPos = positions[rel.to];
            if (!fromBox || !toBox || !fromPos || !toPos) return;

            const fw = fromBox.offsetWidth || BOX_W;
            const fh = fromBox.offsetHeight || BOX_H_EST;
            const tw = toBox.offsetWidth || BOX_W;
            const th = toBox.offsetHeight || BOX_H_EST;

            const fcx = fromPos.x + fw / 2;
            const fcy = fromPos.y + fh / 2;
            const tcx = toPos.x + tw / 2;
            const tcy = toPos.y + th / 2;

            // Edge connection points (so the line touches the box rim, not its centre)
            const p1 = edgePoint(fcx, fcy, fw, fh, tcx, tcy);
            const p2 = edgePoint(tcx, tcy, tw, th, fcx, fcy);

            const dx = p2.x - p1.x;
            const dy = p2.y - p1.y;
            const length = Math.sqrt(dx * dx + dy * dy);
            const angle = Math.atan2(dy, dx) * 180 / Math.PI;

            const line = document.createElement('div');
            line.className = 'erd-line';
            line.style.left = p1.x + 'px';
            line.style.top = p1.y + 'px';
            line.style.width = length + 'px';
            line.style.transform = 'rotate(' + angle + 'deg)';
            linesLayer.appendChild(line);

            // Label at the midpoint, anchored over the mid-point of the rotated line
            const mx = (p1.x + p2.x) / 2;
            const my = (p1.y + p2.y) / 2;
            const label = document.createElement('div');
            label.className = 'erd-line-label';
            const cardSpan = rel.cardinality
                ? '<span class="card"> [' + rel.cardinality + ']</span>'
                : '';
            label.innerHTML =
                '<strong>' + rel.name + '</strong>' + cardSpan + '<br>' + rel.label;
            linesLayer.appendChild(label);
            // Offset so the label's centre sits on (mx, my)
            const lw = label.offsetWidth || 120;
            const lh = label.offsetHeight || 24;
            label.style.left = (mx - lw / 2) + 'px';
            label.style.top = (my - lh / 2) + 'px';
        }});
    }}

    // Apply after DOM paints so offsetWidth/offsetHeight are accurate
    setTimeout(drawLines, 50);
    setTimeout(drawLines, 400);

    window.erdZoom = (factor) => {{
        scale = Math.min(3, Math.max(0.3, scale * factor));
        inner.style.transform = 'scale(' + scale + ')';
    }};
    window.erdReset = () => {{
        scale = 1;
        inner.style.transform = 'scale(1)';
        wrap.scrollTo({{ top: 0, left: 0, behavior: 'smooth' }});
    }};
}})();
</script>
"""

st.components.v1.html(html, height=760, scrolling=False)

# ------------------------------------------------------------------
# Table detail panel
# ------------------------------------------------------------------
st.divider()
st.subheader("📋 Table Details")

detail_tables = tables_df["table_name"].tolist()
if not detail_tables:
    st.info("No tables found in this schema.")
else:
    selected_table = st.selectbox(
        "Select table for details", detail_tables, key="dm_detail_table"
    )
    if selected_table:
        table_cols = columns_df[columns_df["table_name"] == selected_table].copy()
        table_cols["description"] = table_cols["column_name"].apply(describe_vault_column)

        try:
            row_count = get_connection().execute(
                f"SELECT COUNT(*) FROM {schema}.{selected_table}"
            ).fetchone()[0]
        except Exception:
            row_count = 0

        st.markdown(
            f"**{schema}.{selected_table}** — {row_count:,} rows, {len(table_cols)} columns"
        )
        st.dataframe(
            table_cols[["column_name", "data_type", "description"]],
            use_container_width=True,
            hide_index=True,
            height=min(420, 35 * len(table_cols) + 38),
            column_config={
                "column_name": st.column_config.Column("Column", width="medium"),
                "data_type":   st.column_config.Column("Type", width="small"),
                "description": st.column_config.Column("Description", width="large"),
            },
        )

        # Relationship context
        table_rels = [
            r for r in relationships
            if r["from"] == selected_table or r["to"] == selected_table
        ]
        if table_rels:
            st.markdown("**Relationships**")
            for r in table_rels:
                direction = "→" if r["from"] == selected_table else "←"
                other = r["to"] if r["from"] == selected_table else r["from"]
                card = f' [{r["cardinality"]}]' if r.get("cardinality") else ""
                st.markdown(
                    f"- {direction} `{other}` — **{r['name']}** — {r['label']}{card}"
                )
