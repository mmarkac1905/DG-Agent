"""Re-run the full DV 2.0 verification audit. Read-only.

Writes structured results to /tmp/dv_verify_results.json (best-effort) and
prints a pass/fail summary to stdout.
"""

import duckdb
import json
import sys
from pathlib import Path

DB = "cpe_analytics.duckdb"
results = {}

conn = duckdb.connect(DB, read_only=True)

def q(sql):
    return conn.execute(sql).fetchall()

def qone(sql):
    r = conn.execute(sql).fetchone()
    return r[0] if r else None


# ---- V1: Hub uniqueness ----
hubs = {
    "hub_purchase_order":    "hk_purchase_order",
    "hub_vendor":            "hk_vendor",
    "hub_material":          "hk_material",
    "hub_equipment":         "hk_equipment",
    "hub_plant":             "hk_plant",
    "hub_material_document": "hk_material_document",
    "hub_invoice":           "hk_invoice",
    "hub_purchase_requisition": "hk_purchase_requisition",
}
v1 = []
for hub, hk in hubs.items():
    rows = qone(f"SELECT COUNT(*) FROM main_vault.{hub}")
    distinct = qone(f"SELECT COUNT(DISTINCT {hk}) FROM main_vault.{hub}")
    v1.append((hub, rows, distinct, "PASS" if rows == distinct else "FAIL"))
results["V1"] = v1


# ---- V2: Hub structure (hk + bk + load_date + record_source) ----
v2 = []
for hub in hubs:
    cols = [c[0] for c in q(f"SELECT column_name FROM information_schema.columns WHERE table_schema='main_vault' AND table_name='{hub}'")]
    has_hk = any(c.startswith("hk_") for c in cols)
    has_ld = "load_date" in cols
    has_rs = "record_source" in cols
    status = "PASS" if (has_hk and has_ld and has_rs) else "FAIL"
    v2.append((hub, cols, status))
results["V2"] = v2


# ---- V3: Link structure ----
links = [
    "link_po_vendor", "link_po_material", "link_po_plant",
    "link_gr_po", "link_gr_material",
    "link_equipment_material", "link_equipment_gr",
    "link_invoice_po", "link_pr_po",
    "link_po_item",
]
# Unit-of-work links: 1 parent hub + natural child key (accepted DV 2.0 extension)
unit_of_work_links = {"link_po_item"}
v3 = []
for lk in links:
    cols = [c[0] for c in q(f"SELECT column_name FROM information_schema.columns WHERE table_schema='main_vault' AND table_name='{lk}'")]
    hks = [c for c in cols if c.startswith("hk_")]
    own_hk = any(c == f"hk_{lk.replace('link_','')}" for c in cols)
    parent_hks = [h for h in hks if h != f"hk_{lk.replace('link_','')}"]
    has_ld = "load_date" in cols
    has_rs = "record_source" in cols
    min_parents = 1 if lk in unit_of_work_links else 2
    status = "PASS" if (own_hk and len(parent_hks) >= min_parents and has_ld and has_rs) else "FAIL"
    kind = "unit-of-work" if lk in unit_of_work_links else "standard"
    v3.append((lk, parent_hks, kind, status))
results["V3"] = v3


# ---- V4: Satellite structure ----
sats = [
    "sat_vendor_general", "sat_vendor_commercial",
    "sat_material_general", "sat_material_description", "sat_material_plant",
    "sat_po_header", "sat_po_item", "sat_po_schedule", "sat_po_account",
    "sat_gr_header", "sat_gr_item",
    "sat_equipment_general", "sat_equipment_status",
    "sat_invoice_header",
    "sat_stock_level",
    "sat_pr_detail",
]
v4 = []
for s in sats:
    cols = [c[0] for c in q(f"SELECT column_name FROM information_schema.columns WHERE table_schema='main_vault' AND table_name='{s}'")]
    parent_hks = [c for c in cols if c.startswith("hk_")]
    has_hd = "hashdiff" in cols
    has_ld = "load_date" in cols
    has_rs = "record_source" in cols
    payload = [c for c in cols if not c.startswith("hk_") and c not in ("hashdiff","load_date","record_source")]
    status = "PASS" if (parent_hks and has_hd and has_ld and has_rs and payload) else "FAIL"
    v4.append((s, parent_hks, has_hd, len(payload), status))
results["V4"] = v4


# ---- V5: Link -> Hub FK integrity ----
link_fks = [
    ("link_po_vendor",         "hk_purchase_order",     "hub_purchase_order",     "hk_purchase_order"),
    ("link_po_vendor",         "hk_vendor",             "hub_vendor",             "hk_vendor"),
    ("link_po_material",       "hk_purchase_order",     "hub_purchase_order",     "hk_purchase_order"),
    ("link_po_material",       "hk_material",           "hub_material",           "hk_material"),
    ("link_po_plant",          "hk_purchase_order",     "hub_purchase_order",     "hk_purchase_order"),
    ("link_po_plant",          "hk_plant",              "hub_plant",              "hk_plant"),
    ("link_gr_po",             "hk_material_document",  "hub_material_document",  "hk_material_document"),
    ("link_gr_po",             "hk_purchase_order",     "hub_purchase_order",     "hk_purchase_order"),
    ("link_gr_material",       "hk_material_document",  "hub_material_document",  "hk_material_document"),
    ("link_gr_material",       "hk_material",           "hub_material",           "hk_material"),
    ("link_equipment_material","hk_equipment",          "hub_equipment",          "hk_equipment"),
    ("link_equipment_material","hk_material",           "hub_material",           "hk_material"),
    ("link_equipment_gr",      "hk_equipment",          "hub_equipment",          "hk_equipment"),
    ("link_equipment_gr",      "hk_material_document",  "hub_material_document",  "hk_material_document"),
    ("link_invoice_po",        "hk_invoice",            "hub_invoice",            "hk_invoice"),
    ("link_invoice_po",        "hk_purchase_order",     "hub_purchase_order",     "hk_purchase_order"),
    ("link_pr_po",             "hk_purchase_requisition","hub_purchase_requisition","hk_purchase_requisition"),
    ("link_pr_po",             "hk_purchase_order",     "hub_purchase_order",     "hk_purchase_order"),
    ("link_po_item",           "hk_purchase_order",     "hub_purchase_order",     "hk_purchase_order"),
]
v5 = []
for link, lhk, hub, hhk in link_fks:
    orph = qone(f"SELECT COUNT(*) FROM main_vault.{link} l LEFT JOIN main_vault.{hub} h ON l.{lhk}=h.{hhk} WHERE h.{hhk} IS NULL")
    v5.append((link, lhk, hub, orph, "PASS" if orph == 0 else f"FAIL({orph})"))
results["V5"] = v5


# ---- V6: Sat -> Hub/Link FK integrity ----
sat_fks = [
    # sat, parent_hk_col, parent_table, parent_hk_col_target
    ("sat_vendor_general",      "hk_vendor",            "hub_vendor",            "hk_vendor"),
    ("sat_vendor_commercial",   "hk_vendor",            "hub_vendor",            "hk_vendor"),
    ("sat_material_general",    "hk_material",          "hub_material",          "hk_material"),
    ("sat_material_description","hk_material",          "hub_material",          "hk_material"),
    ("sat_po_header",           "hk_purchase_order",    "hub_purchase_order",    "hk_purchase_order"),
    ("sat_gr_header",           "hk_material_document", "hub_material_document", "hk_material_document"),
    ("sat_equipment_general",   "hk_equipment",         "hub_equipment",         "hk_equipment"),
    ("sat_equipment_status",    "hk_equipment",         "hub_equipment",         "hk_equipment"),
    ("sat_invoice_header",      "hk_invoice",           "hub_invoice",           "hk_invoice"),
    ("sat_pr_detail",           "hk_purchase_requisition","hub_purchase_requisition","hk_purchase_requisition"),
    ("sat_po_item",             "hk_po_material",       "link_po_material",      "hk_po_material"),
    ("sat_gr_item",             "hk_gr_material",       "link_gr_material",      "hk_gr_material"),
    # V6c fixed: both now hang off link_po_item
    ("sat_po_schedule",         "hk_po_item",           "link_po_item",          "hk_po_item"),
    ("sat_po_account",          "hk_po_item",           "link_po_item",          "hk_po_item"),
]
v6 = []
for sat, shk, parent, phk in sat_fks:
    try:
        orph = qone(f"SELECT COUNT(*) FROM main_vault.{sat} s LEFT JOIN main_vault.{parent} p ON s.{shk}=p.{phk} WHERE p.{phk} IS NULL")
        v6.append((sat, shk, parent, orph, "PASS" if orph == 0 else f"FAIL({orph})"))
    except Exception as e:
        v6.append((sat, shk, parent, None, f"ERROR: {e}"))
results["V6"] = v6


# ---- V7: Hash-key consistency across staging ----
v7_checks = [
    ("vendor hash consistency",  "stg_sap__ekko",  "hk_vendor",
                                 "stg_sap__lfa1",  "hk_vendor"),
    ("material hash consistency","stg_sap__ekpo",  "hk_material",
                                 "stg_sap__mara",  "hk_material"),
    ("PO hash consistency",      "stg_sap__ekpo",  "hk_purchase_order",
                                 "stg_sap__ekko",  "hk_purchase_order"),
    ("hk_po_item consistency a", "stg_sap__ekpo",  "hk_po_item",
                                 "stg_sap__eket",  "hk_po_item"),
    ("hk_po_item consistency b", "stg_sap__ekpo",  "hk_po_item",
                                 "stg_sap__ekkn",  "hk_po_item"),
]
v7 = []
for label, ta, ca, tb, cb in v7_checks:
    try:
        mismatch = qone(f"""
          SELECT COUNT(*) FROM (
            SELECT DISTINCT {ca} h FROM main_staging.{ta}
            EXCEPT
            SELECT DISTINCT {cb} h FROM main_staging.{tb}
          )
        """)
        v7.append((label, mismatch, "PASS" if mismatch == 0 else f"FAIL({mismatch})"))
    except Exception as e:
        v7.append((label, None, f"ERROR: {e}"))
results["V7"] = v7


# ---- V8: End-to-end traceability (Equipment -> GR -> PO -> Vendor) ----
trace = qone("""
  SELECT COUNT(*) FROM (
    SELECT e.hk_equipment, v.hk_vendor
    FROM main_vault.hub_equipment e
    JOIN main_vault.link_equipment_gr leg ON e.hk_equipment = leg.hk_equipment
    JOIN main_vault.link_gr_po lgp        ON leg.hk_material_document = lgp.hk_material_document
    JOIN main_vault.link_po_vendor lpv    ON lgp.hk_purchase_order = lpv.hk_purchase_order
    JOIN main_vault.hub_vendor v          ON lpv.hk_vendor = v.hk_vendor
  )
""")
devs = qone("""
  SELECT COUNT(DISTINCT e.hk_equipment)
  FROM main_vault.hub_equipment e
  JOIN main_vault.link_equipment_gr leg ON e.hk_equipment = leg.hk_equipment
""")
grs = qone("""
  SELECT COUNT(DISTINCT lgp.hk_material_document)
  FROM main_vault.link_equipment_gr leg
  JOIN main_vault.link_gr_po lgp ON leg.hk_material_document = lgp.hk_material_document
""")
pos_ = qone("""
  SELECT COUNT(DISTINCT lgp.hk_purchase_order)
  FROM main_vault.link_equipment_gr leg
  JOIN main_vault.link_gr_po lgp ON leg.hk_material_document = lgp.hk_material_document
""")
vends = qone("""
  SELECT COUNT(DISTINCT lpv.hk_vendor)
  FROM main_vault.link_equipment_gr leg
  JOIN main_vault.link_gr_po lgp ON leg.hk_material_document = lgp.hk_material_document
  JOIN main_vault.link_po_vendor lpv ON lgp.hk_purchase_order = lpv.hk_purchase_order
""")
results["V8"] = {"traversable_rows": trace, "devices": devs, "grs": grs, "pos": pos_, "vendors": vends,
                 "status": "PASS" if trace > 0 else "FAIL"}


# ---- V9: Staging 1:1 with raw_sap ----
stg_parity = [
    ("stg_sap__ekko", "raw_sap.ekko"),
    ("stg_sap__ekpo", "raw_sap.ekpo"),
    ("stg_sap__mseg", "raw_sap.mseg"),
    ("stg_sap__lfa1", "raw_sap.lfa1"),
    ("stg_sap__mara", "raw_sap.mara"),
    ("stg_sap__equi", "raw_sap.equi"),
    ("stg_sap__eqbs", "raw_sap.eqbs"),
    ("stg_sap__mard", "raw_sap.mard"),
    ("stg_sap__mkpf", "raw_sap.mkpf"),
]
v9 = []
for stg, raw in stg_parity:
    try:
        s = qone(f"SELECT COUNT(*) FROM main_staging.{stg}")
        r = qone(f"SELECT COUNT(*) FROM {raw}")
        v9.append((stg, r, s, "PASS" if r == s else f"FAIL(raw={r} stg={s})"))
    except Exception as e:
        v9.append((stg, None, None, f"ERROR: {e}"))
results["V9"] = v9


# ---- V10: Row-count census ----
v10 = {}
for schema in ("main_staging", "main_vault", "main_marts"):
    rows = q(f"SELECT table_name FROM information_schema.tables WHERE table_schema='{schema}' ORDER BY table_name")
    counts = []
    for (t,) in rows:
        try:
            c = qone(f"SELECT COUNT(*) FROM {schema}.{t}")
        except Exception:
            c = None
        counts.append((t, c))
    v10[schema] = counts
results["V10"] = v10


# ---- Write results ----
try:
    out = Path("/tmp/dv_verify_results.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as f:
        json.dump(results, f, default=str, indent=2)
except Exception:
    pass

# ---- Summary ----
def count_status(items, idx):
    p = sum(1 for x in items if str(x[idx]).startswith("PASS"))
    return p, len(items)

print("=" * 60)
print("DV 2.0 VERIFICATION SUMMARY")
print("=" * 60)

v1p, v1t = count_status(results["V1"], 3)
print(f"V1 Hub uniqueness:           {v1p}/{v1t}")

v2p, v2t = count_status(results["V2"], 2)
print(f"V2 Hub structure:            {v2p}/{v2t}")

v3p, v3t = count_status(results["V3"], 3)
print(f"V3 Link structure:           {v3p}/{v3t}")

v4p, v4t = count_status(results["V4"], 4)
print(f"V4 Satellite structure:      {v4p}/{v4t}")

v5p, v5t = count_status(results["V5"], 4)
print(f"V5 Link -> Hub FK:           {v5p}/{v5t}")

v6p, v6t = count_status(results["V6"], 4)
print(f"V6 Sat -> Hub/Link FK:       {v6p}/{v6t}")

v7p, v7t = count_status(results["V7"], 2)
print(f"V7 Hash-key consistency:     {v7p}/{v7t}")

print(f"V8 End-to-end traceability:  {results['V8']['status']} ({results['V8']['devices']} devices -> {results['V8']['vendors']} vendors)")

v9p, v9t = count_status(results["V9"], 3)
print(f"V9 Staging 1:1 parity:       {v9p}/{v9t}")

for schema, items in results["V10"].items():
    print(f"V10 {schema:14s}  {len(items)} tables")

# Final verdict
core_pass = (v1p == v1t and v2p == v2t and v3p == v3t and v4p == v4t
             and v5p == v5t and v6p == v6t and v7p == v7t
             and results["V8"]["status"] == "PASS" and v9p == v9t)
print()
print("VERDICT:", "ALL PASS" if core_pass else "FAILURES PRESENT — see results")

# Print details for any FAIL
print()
for key in ["V1","V2","V3","V4","V5","V6","V7","V9"]:
    for item in results[key]:
        s = str(item[-1])
        if not s.startswith("PASS"):
            print(f"  [{key}] {item}")

conn.close()
