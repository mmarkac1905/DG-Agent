"""Extract model relationships from dbt metadata.

Source of truth for the Data Model ERD page. Generates two kinds of edges:

1. PIPELINE edges — follow `{{ ref() }}` calls between models, giving the
   DAG that dbt actually builds. Covers staging → source, vault → staging,
   mart → vault, obt → mart, knowledge → anything.

2. STAR edges — in the marts layer, Kimball joins between facts and dims
   are not expressed via `ref()` (facts and dims both read from vault
   primitives, not from each other). Instead we use dbt_column_lineage:
   for every mart column we walk origin_table/origin_column hops until we
   land on a `raw_sap.TABLE.FIELD` leaf; two mart models that share the
   same raw SAP key field (WERKS, LIFNR, MATNR, …) are joinable through
   that field. A small special-case also connects every fact with a
   `*_date` column to dim_date, which has no raw_sap lineage of its own.

Output: dbt/seeds/dbt_model_relationships.csv

The Data Model page reads this seed and merges it with the curated
MART_RELATIONSHIPS list so any edges the extractor genuinely can't derive
(e.g. fact_equipment_lifecycle.plant → GEWRK, not WERKS) still render.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "dbt" / "models"
SEED_DIR = ROOT / "dbt" / "seeds"
LINEAGE_CSV = SEED_DIR / "dbt_column_lineage.csv"
OUTPUT = SEED_DIR / "dbt_model_relationships.csv"

# SAP key fields worth treating as join keys. Anything outside this
# whitelist is ignored when computing star-schema edges so we don't draw
# lines on accidental field-name collisions (e.g. MANDT showing up in
# every table).
SAP_KEY_FIELDS = {
    "WERKS",   # plant code
    "LIFNR",   # vendor account
    "MATNR",   # material number
    "EBELN",   # purchase order document
    "EBELP",   # PO item
    "EQUNR",   # equipment number
    "LGORT",   # storage location
    "MBLNR",   # material document
    "BANFN",   # purchase requisition
    "BELNR",   # accounting document
    "MJAHR",   # material doc fiscal year
    "GJAHR",   # accounting fiscal year
    "BWART",   # movement type code
}

_REF_RE = re.compile(r"\{\{\s*ref\s*\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}")
_SOURCE_RE = re.compile(
    r"\{\{\s*source\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}"
)


def _layer_from_path(path: Path) -> str:
    parts = str(path.relative_to(MODELS_DIR)).replace("\\", "/").split("/")
    head = parts[0] if parts else ""
    if head in {"staging", "vault", "marts", "obt", "knowledge"}:
        return head
    return "other"


def load_models():
    """Scan dbt/models/**/*.sql and return a list of dicts describing each
    model: name, layer, refs, sources."""
    models = []
    for sql_file in sorted(MODELS_DIR.rglob("*.sql")):
        sql = sql_file.read_text(encoding="utf-8")
        refs = _REF_RE.findall(sql)
        sources = _SOURCE_RE.findall(sql)
        models.append({
            "name": sql_file.stem,
            "layer": _layer_from_path(sql_file),
            "refs": refs,
            "sources": sources,
        })
    return models


def load_lineage_index():
    """Return {(model_name, column_name): row} built from the lineage seed."""
    if not LINEAGE_CSV.exists():
        return {}
    idx = {}
    with LINEAGE_CSV.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = (row["model_name"], row["column_name"])
            idx[key] = row
    return idx


def deep_raw_origin(model, col, index, depth=0):
    """Walk origin_table/origin_column hops until we hit a raw_sap.* leaf
    or run out of chain. Returns (raw_table, raw_field) or None."""
    if depth > 10:
        return None
    row = index.get((model, col))
    if not row:
        return None
    ot = (row.get("origin_table") or "").strip()
    oc = (row.get("origin_column") or "").strip()
    if not ot or not oc:
        return None
    if ot.startswith("raw_sap."):
        return (ot.split(".", 1)[1], oc)
    return deep_raw_origin(ot, oc, index, depth + 1)


def collect_raw_fields_per_model(models, lineage_index):
    """For every model, return {model_name: set of raw SAP field names reachable
    via column lineage}."""
    all_columns = {}
    for key in lineage_index:
        all_columns.setdefault(key[0], []).append(key[1])

    out = {}
    for m in models:
        name = m["name"]
        fields = set()
        for col in all_columns.get(name, []):
            raw = deep_raw_origin(name, col, lineage_index)
            if raw:
                _raw_tbl, raw_field = raw
                if raw_field.upper() in SAP_KEY_FIELDS:
                    fields.add(raw_field.upper())
        out[name] = fields
    return out


def extract_pipeline_edges(models):
    """Emit one edge per `ref()` call. Also emit source-table edges for the
    staging → raw_sap boundary."""
    edges = []
    for m in models:
        for target in m["refs"]:
            edges.append({
                "from_model": m["name"],
                "from_layer": m["layer"],
                "to_model": target,
                "to_layer": "",  # resolved below
                "relationship_type": "pipeline",
                "join_key": "",
                "label": "reads from",
            })
        for schema, table in m["sources"]:
            edges.append({
                "from_model": m["name"],
                "from_layer": m["layer"],
                "to_model": f"{schema}.{table}",
                "to_layer": "raw",
                "relationship_type": "source",
                "join_key": "",
                "label": f"reads from {schema}.{table}",
            })
    # Resolve to_layer for ref edges
    layer_by_name = {m["name"]: m["layer"] for m in models}
    for e in edges:
        if e["to_layer"]:
            continue
        e["to_layer"] = layer_by_name.get(e["to_model"], "other")
    return edges


def _dim_identity_field(dim_model, lineage_index):
    """Return the SAP raw field that represents a dimension's identity —
    i.e. the deep origin of its hk_* column (the primary key). Returns
    None if the dim has no hash key (e.g. generated calendars)."""
    for (model, col), row in lineage_index.items():
        if model != dim_model or not col.startswith("hk_"):
            continue
        raw = deep_raw_origin(model, col, lineage_index)
        if raw:
            return raw[1].upper()
    return None


def extract_star_edges(models, raw_fields_by_model, lineage_index):
    """In the marts layer, emit fact ↔ dim edges whenever the fact carries
    the dim's primary-key SAP field in its column lineage.

    Earlier versions matched on ANY shared key field, which over-connected
    dim_equipment to every fact carrying MATNR (dim_equipment's identity
    is EQUNR; MATNR is just an attribute). Restricting to the dim's
    identity field guarantees 1:N parent-child star joins only."""
    edges = []
    marts = [m for m in models if m["layer"] == "marts"]
    facts = [m for m in marts if m["name"].startswith("fact_")]
    dims = [m for m in marts if m["name"].startswith("dim_")]

    # Precompute each dim's identity field (or fall back to the single
    # raw field it carries, when there's exactly one — covers simple
    # one-hub dims like dim_plant/dim_vendor/dim_material).
    dim_identity = {}
    for d in dims:
        ident = _dim_identity_field(d["name"], lineage_index)
        if not ident:
            fields = raw_fields_by_model.get(d["name"], set())
            ident = next(iter(fields)) if len(fields) == 1 else None
        if ident:
            dim_identity[d["name"]] = ident

    label_by_field = {
        "WERKS": "joined on plant",
        "LIFNR": "joined on vendor",
        "MATNR": "joined on material",
        "EBELN": "joined on purchase order",
        "EBELP": "joined on PO item",
        "EQUNR": "joined on equipment",
        "LGORT": "joined on storage location",
        "MBLNR": "joined on material document",
        "BANFN": "joined on purchase requisition",
        "BWART": "joined on movement type",
    }

    for f in facts:
        fact_fields = raw_fields_by_model.get(f["name"], set())
        for d in dims:
            identity = dim_identity.get(d["name"])
            if not identity or identity not in fact_fields:
                continue
            edges.append({
                "from_model": f["name"],
                "from_layer": "marts",
                "to_model": d["name"],
                "to_layer": "marts",
                "relationship_type": "star",
                "join_key": identity,
                "label": label_by_field.get(identity, f"joined on {identity}"),
            })
    return edges


def extract_date_edges(models, lineage_index):
    """Special case: dim_date is a generated calendar with no raw SAP
    lineage. Connect every fact that carries a `*_date` column to it."""
    dim_date_exists = any(
        m["name"] == "dim_date" and m["layer"] == "marts" for m in models
    )
    if not dim_date_exists:
        return []

    cols_by_model = {}
    for (model, col) in lineage_index:
        cols_by_model.setdefault(model, set()).add(col)

    edges = []
    for m in models:
        if m["layer"] != "marts" or not m["name"].startswith("fact_"):
            continue
        cols = cols_by_model.get(m["name"], set())
        date_cols = sorted(c for c in cols if c.endswith("_date"))
        if not date_cols:
            continue
        # One edge per fact — don't fan out per date column, that would
        # put 3+ parallel lines on the diagram. Use the most common
        # "primary" date if we can spot it.
        primary = (
            "po_date" if "po_date" in date_cols
            else "posting_date" if "posting_date" in date_cols
            else "invoice_date" if "invoice_date" in date_cols
            else "status_from_date" if "status_from_date" in date_cols
            else date_cols[0]
        )
        edges.append({
            "from_model": m["name"],
            "from_layer": "marts",
            "to_model": "dim_date",
            "to_layer": "marts",
            "relationship_type": "date",
            "join_key": primary,
            "label": f"joined on {primary}",
        })
    return edges


def main():
    models = load_models()
    lineage_index = load_lineage_index()
    if not lineage_index:
        print("WARN: dbt_column_lineage.csv is empty — run scan_dbt_models.py first.")

    raw_by_model = collect_raw_fields_per_model(models, lineage_index)

    pipeline_edges = extract_pipeline_edges(models)
    star_edges = extract_star_edges(models, raw_by_model, lineage_index)
    date_edges = extract_date_edges(models, lineage_index)

    all_edges = pipeline_edges + star_edges + date_edges

    # Stable row ids
    for i, e in enumerate(all_edges, start=1):
        e["id"] = f"R{i:04d}"

    fieldnames = [
        "id", "from_model", "from_layer", "to_model", "to_layer",
        "relationship_type", "join_key", "label",
    ]
    with OUTPUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        for e in all_edges:
            w.writerow({k: e.get(k, "") for k in fieldnames})

    # Summary
    by_type = {}
    for e in all_edges:
        by_type[e["relationship_type"]] = by_type.get(e["relationship_type"], 0) + 1
    print(f"Extracted {len(all_edges)} relationships -> {OUTPUT.relative_to(ROOT)}")
    for t, n in sorted(by_type.items()):
        print(f"  {t:9} {n}")

    # Show star edges involving the four dims the user cares about
    print("\nStar + date edges (marts layer):")
    for e in star_edges + date_edges:
        target = e["to_model"]
        if target in {"dim_plant", "dim_vendor", "dim_material", "dim_date"}:
            print(f"  {e['from_model']:30s} -> {target:20s} via {e['join_key']}")


if __name__ == "__main__":
    main()
