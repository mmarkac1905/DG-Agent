"""Layer B dbt semantic model compiler.

Deterministic extraction from dbt/target/manifest.json. No LLM calls —
the manifest is structured ground truth for every model (columns, types,
tests, depends_on, materialization, description). Extraction IS the
compilation.

Complements Layer A (scripts/compile_semantic_model.py), which uses
LLM synthesis to produce conventions for raw tables WITHOUT dbt
ontology coverage. Layer B is the per-dbt-model equivalent.

CLI:
  python scripts/compile_dbt_semantic_model.py                     # default
  python scripts/compile_dbt_semantic_model.py --force             # bypass staleness check
  python scripts/compile_dbt_semantic_model.py --manifest-path <p> # override manifest path

Invariants:
- LF line endings (anti-pattern #48 / #50)
- csv-safeguard boundary (anti-pattern #56)
- DictWriter fieldnames pre-validated (anti-pattern #57)
- conn param pattern (anti-pattern #31)
- RULE 36 timestamp formatting
- Human-override rows preserved across recompile

reference_sql is stored in dbt canonical Jinja form (FROM {{ ref() }}).
The assembler rewrites to literal schema-qualified form for iteration
consumers.

Exit codes:
  0 — success (compiled or skipped due to unchanged manifest hash)
  1 — manifest.json missing or malformed
  2 — CSV write refused by safeguard
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional

import duckdb

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SEED_DIR = _PROJECT_ROOT / "dbt" / "seeds"
_DBT_DIR = _PROJECT_ROOT / "dbt"
_DEFAULT_MANIFEST = _DBT_DIR / "target" / "manifest.json"
_DB_PATH = _PROJECT_ROOT / "cpe_analytics.duckdb"
_DBT_SEMANTIC_CSV = _SEED_DIR / "dbt_semantic_model.csv"

sys.path.insert(0, str(_PROJECT_ROOT / "app"))
from _csv_safeguard import (  # noqa: E402
    assert_csv_safe_row_count,
    assert_fieldnames_cover_rows,
)

# Column order locked to dbt/seeds/dbt_semantic_model.csv header +
# schema.yml column_types. Mirror any change across all three files.
DBT_SEMANTIC_COLUMNS: list[str] = [
    "model_name",
    "dbt_layer",
    "materialized",
    "upstream_models",
    "downstream_models",
    "exposed_columns_json",
    "primary_key_cols",
    "canonical_alias",
    "typical_join_keys_json",
    "reference_sql",
    "model_description",
    "populated_at_utc",
    "populated_by",
    "source_manifest_hash",
]

# Prefix → dbt_layer heuristic. Order matters: specific prefixes first
# (stg_sap__ must match before a hypothetical 'stg_' alone).
_LAYER_PREFIXES: list[tuple[str, str]] = [
    ("stg_", "staging"),
    ("hub_", "vault_hub"),
    ("link_", "vault_link"),
    ("sat_", "vault_satellite"),
    ("fact_", "mart_fact"),
    ("dim_", "mart_dim"),
    ("obt_", "obt"),
    ("knowledge_", "knowledge"),
]


# ─── Utility ──────────────────────────────────────────────────────────


def _now_utc_naive() -> dt.datetime:
    """RULE 36 / anti-pattern #54 — tz-naive UTC for DuckDB TIMESTAMP."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return str(value)


def _compute_manifest_hash(manifest_path: Path) -> str:
    h = hashlib.sha256()
    with manifest_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── Classification heuristics ────────────────────────────────────────


def classify_dbt_layer(name: str) -> str:
    n = name.lower()
    for prefix, layer in _LAYER_PREFIXES:
        if n.startswith(prefix):
            return layer
    return "other"


def derive_canonical_alias(name: str, dbt_layer: str) -> str:
    n = name.lower()
    # Strip the matched prefix. For stg_sap__ekbe, prefix is 'stg_' then the
    # remainder 'sap__ekbe' — further strip 'sap__' to yield 'ekbe'. Makes
    # the alias match the raw SAP table name for staging models.
    for prefix, layer in _LAYER_PREFIXES:
        if dbt_layer == layer and n.startswith(prefix):
            stripped = n[len(prefix):]
            if stripped.startswith("sap__"):
                stripped = stripped[len("sap__"):]
            return stripped or n
    return n


# ─── Manifest extraction ──────────────────────────────────────────────


def _short_name(unique_id: str) -> str:
    """Extract final name from unique_id like
    'model.cpe_procurement_analytics.stg_sap__ekpo' → 'stg_sap__ekpo'.
    Sources: 'source.project.raw_sap.ekpo' → 'raw_sap.ekpo'.
    """
    parts = unique_id.split(".")
    if unique_id.startswith("source.") and len(parts) >= 4:
        # source.project.schema.name → schema.name
        return ".".join(parts[-2:])
    return parts[-1]


def _extract_column_tests(manifest: dict, model_unique_id: str,
                          model_name: str) -> dict[str, list[str]]:
    """Cross-reference manifest test nodes to determine which tests
    apply to each column of the given model. Returns
    {column_name: [test_name_or_type, ...]}.
    """
    tests_by_col: dict[str, list[str]] = {}
    for nid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "test":
            continue
        # Test nodes carry `test_metadata` with name/kwargs; `depends_on.nodes`
        # points at models; `column_name` field (when present) targets a column.
        depends = node.get("depends_on", {}).get("nodes", []) or []
        if model_unique_id not in depends:
            continue
        col = node.get("column_name")
        if not col:
            continue
        test_name = (node.get("test_metadata") or {}).get("name") or node.get("name", "")
        tests_by_col.setdefault(col, []).append(test_name)
    return tests_by_col


def _extract_pk_cols(node: dict, column_tests: dict[str, list[str]]) -> str:
    """PK = columns with a `unique` (and typically also `not_null`) test,
    or the value of `config.unique_key` if set."""
    config = node.get("config") or {}
    uk = config.get("unique_key")
    if isinstance(uk, list):
        return ",".join(str(x) for x in uk)
    if isinstance(uk, str) and uk:
        return uk
    pks = sorted(col for col, tests in column_tests.items() if "unique" in tests)
    return ",".join(pks)


def _derive_typical_join_keys(
    node: dict,
    manifest_nodes: dict,
    upstream_ids: list[str],
) -> dict:
    """Heuristic: for each upstream model, check whether this node's
    columns have a `relationships` test pointing at that upstream. If so,
    record the column(s) as the join key.
    """
    joins: dict[str, list[str]] = {}
    this_unique_id = None
    for nid, n in manifest_nodes.items():
        if n is node:
            this_unique_id = nid
            break
    if not this_unique_id:
        return joins

    for upstream_id in upstream_ids:
        upstream_short = _short_name(upstream_id)
        # Scan all test nodes whose depends_on includes BOTH this_unique_id
        # and upstream_id and whose test_metadata.name == 'relationships'
        for tid, t in manifest_nodes.items():
            if t.get("resource_type") != "test":
                continue
            deps = t.get("depends_on", {}).get("nodes", []) or []
            if this_unique_id not in deps or upstream_id not in deps:
                continue
            meta = t.get("test_metadata") or {}
            if meta.get("name") != "relationships":
                continue
            col = t.get("column_name")
            if col:
                joins.setdefault(upstream_short, []).append(col)
    return joins


def _render_reference_sql(
    name: str,
    dbt_layer: str,
    canonical_alias: str,
    exposed_cols: dict,
    pk_cols: str,
) -> str:
    """3-5 line ref()-based exemplar in dbt canonical Jinja form.
    Assembler rewrites for iteration consumers; Create S2T consumes as-is.
    """
    cols = list(exposed_cols.keys())
    top = cols[:3] if len(cols) >= 3 else (cols or ["*"])
    alias = canonical_alias or name
    select_list = ", ".join(f"{alias}.{c}" for c in top) if top != ["*"] else f"{alias}.*"
    if dbt_layer in ("mart_fact", "obt"):
        measure = next((c for c in cols if any(k in c.lower() for k in
                        ("amount", "value", "count", "qty", "total", "netwr", "menge"))),
                       cols[0] if cols else None)
        if pk_cols and measure:
            pk_expr = ", ".join(f"{alias}.{c.strip().lower()}" for c in pk_cols.split(","))
            return (
                f"SELECT {pk_expr}, {alias}.{measure}, COUNT(*) AS row_count\n"
                f"  FROM {{{{ ref('{name}') }}}} AS {alias}\n"
                f" GROUP BY {pk_expr}, {alias}.{measure}"
            )
    if dbt_layer in ("mart_dim", "vault_hub", "knowledge"):
        return (
            f"SELECT {select_list}\n"
            f"  FROM {{{{ ref('{name}') }}}} AS {alias}"
        )
    # staging / vault_link / vault_satellite / other — generic SELECT
    return (
        f"SELECT {select_list}\n"
        f"  FROM {{{{ ref('{name}') }}}} AS {alias}"
    )


def build_rows(manifest: dict, manifest_hash: str) -> list[dict]:
    """One row per dbt model node. Deterministic — no LLM."""
    now = _now_utc_naive()
    rows: list[dict] = []

    # Pre-compute reverse-lookup: unique_id → list of downstream unique_ids.
    nodes = manifest.get("nodes", {})
    downstream_map: dict[str, list[str]] = {}
    for nid, node in nodes.items():
        if node.get("resource_type") != "model":
            continue
        for upstream in node.get("depends_on", {}).get("nodes", []) or []:
            downstream_map.setdefault(upstream, []).append(nid)

    for nid, node in sorted(nodes.items()):
        if node.get("resource_type") != "model":
            continue
        name = node.get("name", "").lower()
        if not name:
            continue

        dbt_layer = classify_dbt_layer(name)
        materialized = (node.get("config") or {}).get("materialized") or "view"
        description = (node.get("description") or "").strip()

        upstream_ids = node.get("depends_on", {}).get("nodes", []) or []
        upstream_shorts = sorted({_short_name(uid) for uid in upstream_ids})
        downstream_ids = downstream_map.get(nid, [])
        downstream_shorts = sorted({_short_name(did) for did in downstream_ids})

        column_tests = _extract_column_tests(manifest, nid, name)
        raw_cols = node.get("columns") or {}
        exposed: dict[str, dict] = {}
        for col_name, col_meta in raw_cols.items():
            exposed[col_name] = {
                "type": (col_meta.get("data_type") or "").strip(),
                "description": (col_meta.get("description") or "").strip(),
                "tests": sorted(column_tests.get(col_name, [])),
            }

        pk_cols = _extract_pk_cols(node, column_tests)
        canonical_alias = derive_canonical_alias(name, dbt_layer)
        joins = _derive_typical_join_keys(node, nodes, upstream_ids)
        reference_sql = _render_reference_sql(
            name, dbt_layer, canonical_alias, exposed, pk_cols
        )

        rows.append({
            "model_name": name,
            "dbt_layer": dbt_layer,
            "materialized": materialized,
            "upstream_models": ",".join(upstream_shorts),
            "downstream_models": ",".join(downstream_shorts),
            "exposed_columns_json": json.dumps(exposed, sort_keys=True),
            "primary_key_cols": pk_cols,
            "canonical_alias": canonical_alias,
            "typical_join_keys_json": json.dumps(
                {k: sorted(set(v)) for k, v in joins.items()}, sort_keys=True
            ),
            "reference_sql": reference_sql,
            "model_description": description,
            "populated_at_utc": now,
            "populated_by": "dbt_manifest_compile",
            "source_manifest_hash": manifest_hash,
        })
    return rows


# ─── Existing-row preservation ────────────────────────────────────────


def load_existing_rows(csv_path: Path) -> dict[str, dict]:
    """Returns {model_name: row_dict}."""
    if not csv_path.exists():
        return {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        return {r["model_name"].lower(): r
                for r in csv.DictReader(f) if r.get("model_name")}


def is_human_protected(row: dict) -> bool:
    return row.get("populated_by") == "human_override"


# ─── Atomic CSV write ─────────────────────────────────────────────────


def write_csv(csv_path: Path, rows: list[dict]) -> None:
    assert_fieldnames_cover_rows(DBT_SEMANTIC_COLUMNS, rows)
    assert_csv_safe_row_count(csv_path, len(rows))
    tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=DBT_SEMANTIC_COLUMNS, lineterminator="\n"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _iso(row.get(col, "")) for col in DBT_SEMANTIC_COLUMNS})
    os.replace(tmp, csv_path)


# ─── Orchestration ────────────────────────────────────────────────────


def compile_all(
    conn,
    manifest_path: Path,
    force: bool = False,
) -> dict:
    if not manifest_path.exists():
        print(f"ERROR: manifest not found at {manifest_path}. "
              f"Run 'dbt parse' first to generate it.")
        return {"status": "manifest_missing"}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: manifest.json malformed: {e}")
        return {"status": "manifest_malformed"}

    manifest_hash = _compute_manifest_hash(manifest_path)
    print(f"manifest.json sha256: {manifest_hash[:16]}...")

    existing = load_existing_rows(_DBT_SEMANTIC_CSV)
    if existing and not force:
        # Staleness check — if first non-override row has same hash, skip.
        non_override = [r for r in existing.values() if not is_human_protected(r)]
        if non_override and non_override[0].get("source_manifest_hash") == manifest_hash:
            print(f"manifest hash unchanged ({len(non_override)} rows already compiled). "
                  f"Use --force to recompile anyway.")
            return {"status": "unchanged", "rows": len(existing)}

    preserved = {k: v for k, v in existing.items() if is_human_protected(v)}

    new_rows = build_rows(manifest, manifest_hash)
    print(f"compiled {len(new_rows)} rows from manifest")

    # Distribution by layer for log visibility
    from collections import Counter
    dist = Counter(r["dbt_layer"] for r in new_rows)
    for layer, n in sorted(dist.items()):
        print(f"  {layer}: {n}")

    # Merge: new rows override auto_generated, preserved rows keep their content
    final_by_name: dict[str, dict] = {r["model_name"]: r for r in new_rows}
    for k, v in preserved.items():
        final_by_name[k] = v

    sorted_rows = [final_by_name[k] for k in sorted(final_by_name.keys())]

    try:
        write_csv(_DBT_SEMANTIC_CSV, sorted_rows)
        print(f"\nWrote {len(sorted_rows)} rows to {_DBT_SEMANTIC_CSV}")
    except RuntimeError as e:
        print(f"ERROR: write refused by safeguard: {e}")
        return {"status": "safeguard_block"}

    return {
        "status": "ok",
        "rows": len(sorted_rows),
        "distribution": dict(dist),
        "manifest_hash": manifest_hash,
        # Signal to caller: re-seed after closing its conn (DuckDB is
        # single-writer; subprocess dbt seed can't grab the file while
        # a parent conn holds it open).
        "reseed_after_close": True,
    }


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compile Layer B dbt_semantic_model.csv from manifest.json."
    )
    p.add_argument("--force", action="store_true",
                   help="Recompile even if manifest hash unchanged.")
    p.add_argument("--manifest-path", type=str, default=None,
                   help=f"Path to manifest.json (default: {_DEFAULT_MANIFEST}).")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None, conn=None) -> int:
    args = _parse_args(argv)
    manifest_path = Path(args.manifest_path) if args.manifest_path else _DEFAULT_MANIFEST

    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB_PATH))
    try:
        result = compile_all(conn, manifest_path, force=args.force)
    finally:
        if owned:
            conn.close()

    # KI-106 fix: replace subprocess `dbt seed` + bulk parquet sync with
    # in-process `sync_parquet_and_invalidate(seed_name=...)`. The helper
    # does CREATE OR REPLACE TABLE from CSV (equivalent to dbt seed) and
    # parquet export atomically — no subprocess, no fragile lock contention,
    # no silent failure. Same fix-class as KI-103/KI-105. Original comment
    # about "AFTER closing our conn" still applies because the helper
    # opens its own writer conn when conn=None (default).
    if owned and result.get("reseed_after_close"):
        print("\nRe-seeding dbt_semantic_model into DuckDB (in-process)...")
        from _parquet_sync import sync_parquet_and_invalidate  # noqa: E402
        sync_warning = sync_parquet_and_invalidate(
            project_root=_PROJECT_ROOT,
            seed_name="dbt_semantic_model",
            source="compile_dbt_semantic_model",
        )
        if sync_warning:
            print(
                f"  WARN: dbt_semantic_model sync incomplete: {sync_warning}",
                file=sys.stderr,
            )
        else:
            print("  in-process re-seed + parquet sync OK")

    status = result.get("status")
    if status in ("manifest_missing", "manifest_malformed"):
        return 1
    if status == "safeguard_block":
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
