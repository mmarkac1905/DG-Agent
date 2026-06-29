"""Phase 15b Piece 8 §25.3 (v3.9, 8.4.6) — segmentation_threshold analyzer.

Emits `domain_analysis_results` rows with analysis_type='segmentation_threshold'
for each numeric measure column in each raw source table. Feeds Layer A's
natural_thresholds_json field via compile_semantic_model.py co-compilation.

MVP uses quantile-based thresholds [P25, P50, P75] per §25.9(e). Rationale
string documents the distribution assumption. KDE-based local-minima
detection for multimodal distributions is deferred as a post-launch
enhancement (requires scipy dependency).

Deterministic — no LLM calls. Pure SQL aggregation per column:
  - QUANTILE_CONT at 0.25, 0.50, 0.75 → [t1, t2, t3] threshold triple
  - Distribution sanity: if MIN == MAX or stddev == 0, skip column
    (no meaningful thresholds for constant columns)

CLI:
  python scripts/run_segmentation_analysis.py                      # all raw tables
  python scripts/run_segmentation_analysis.py --tables ekpo,ekko   # subset

Exit codes:
  0 — success (any number of DARs emitted)
  1 — DuckDB error
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

from _dar_supersede import supersede_prior_dars_for_table

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SEED_DIR = _PROJECT_ROOT / "dbt" / "seeds"
_DB_PATH = _PROJECT_ROOT / "cpe_analytics.duckdb"
_DAR_CSV = _SEED_DIR / "domain_analysis_results.csv"

_DAR_FIELDS: list[str] = [
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name",
    "last_source_ingestion_at",
]

_ANALYSIS_TYPE = "segmentation_threshold"
_DOMAIN_NAME = "segmentation"

_NUMERIC_TYPES = frozenset({
    "DECIMAL", "DOUBLE", "INTEGER", "BIGINT", "FLOAT", "REAL",
    "NUMERIC", "HUGEINT", "SMALLINT", "TINYINT", "UBIGINT",
    "UINTEGER", "USMALLINT", "UTINYINT",
})


def _now_utc_naive() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    return str(value)


def _next_dar_id() -> str:
    if not _DAR_CSV.exists():
        return "DAR-00001"
    with _DAR_CSV.open("r", encoding="utf-8", newline="") as f:
        ids = [r.get("id", "") for r in csv.DictReader(f)]
    nums = [int(i.split("-")[1]) for i in ids if i.startswith("DAR-")]
    return f"DAR-{(max(nums) + 1 if nums else 1):05d}"


def _schema_version(conn, table: str) -> str:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    payload = ",".join(f"{c}:{t}" for c, t in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _raw_sap_tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='raw_sap' ORDER BY table_name"
    ).fetchall()
    return [r[0].lower() for r in rows]


def _numeric_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    out: list[str] = []
    for name, ty in rows:
        up = (ty or "").upper()
        if up in _NUMERIC_TYPES or up.startswith("DECIMAL"):
            out.append(name)
    return out


def _analyze_column(conn, table: str, col: str) -> Optional[dict]:
    """Returns finding dict (thresholds + rationale) or None if column
    has no usable distribution (constant / all-null)."""
    safe_col = f'"{col}"'
    safe_table = f'raw_sap."{table}"'
    try:
        r = conn.execute(
            f"SELECT "
            f"  COUNT({safe_col}) AS n, "
            f"  MIN({safe_col}) AS mn, "
            f"  MAX({safe_col}) AS mx, "
            f"  STDDEV_POP({safe_col}) AS sd, "
            f"  QUANTILE_CONT({safe_col}, 0.25) AS p25, "
            f"  QUANTILE_CONT({safe_col}, 0.50) AS p50, "
            f"  QUANTILE_CONT({safe_col}, 0.75) AS p75 "
            f"FROM {safe_table}"
        ).fetchone()
    except Exception as e:
        print(f"    [skip] {table}.{col}: {e}")
        return None

    n, mn, mx, sd, p25, p50, p75 = r
    if not n or mn is None or mx is None:
        return None
    try:
        if mn == mx:
            return None  # constant column — no segmentation meaningful
        if sd is not None and float(sd) == 0.0:
            return None
    except Exception:
        pass

    def _num(v):
        if v is None:
            return None
        try:
            return float(v)
        except Exception:
            return None

    thresholds = [_num(p25), _num(p50), _num(p75)]
    # Filter out all-None / degenerate triples
    if not all(t is not None for t in thresholds):
        return None

    return {
        "col_name": col,
        "thresholds": thresholds,
        "rationale": (
            "quartile-based on distribution assumed unimodal (MVP heuristic per "
            "§25.9(e); KDE-based local-minima detection deferred as post-launch "
            "enhancement for multimodal distributions)."
        ),
    }


def _append_dar(row: dict) -> None:
    existing: list[dict] = []
    if _DAR_CSV.exists():
        with _DAR_CSV.open("r", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    existing.append(row)
    tmp = _DAR_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_DAR_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, "") for k in _DAR_FIELDS})
    os.replace(tmp, _DAR_CSV)


def analyze_table(conn, table: str) -> int:
    """Analyze one raw table; emit one DAR per numeric column. Returns count emitted.

    Stage D.1: when no numeric columns exist, emit a canonical skipped DAR
    instead of silently returning. Prereq counts skipped DARs as satisfying
    the 'segmentation' requirement.
    """
    cols = _numeric_columns(conn, table)
    schema_hash = _schema_version(conn, table)
    if not cols:
        print(f"  [skip] {table}: no numeric columns")
        from _skipped_dar import build_skipped_dar_row  # noqa: E402
        skipped = build_skipped_dar_row(
            dar_id=_next_dar_id(),
            analysis_type=_ANALYSIS_TYPE,
            source_tables=table,
            skip_reason="no numeric columns in table",
            schema_version=schema_hash,
            last_source_ingestion_at="",
            executed_by="run_segmentation_analysis.py",
            domain_name=_DOMAIN_NAME,
        )
        _append_dar(skipped)
        supersede_prior_dars_for_table(
            skipped["analysis_type"], skipped["source_tables"],
            [skipped["id"]],
        )
        print(f"  [ok] {skipped['id']} segmentation_threshold {table}: SKIPPED")
        return 1

    run_id = f"segmentation_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{table}"
    executed = _now_utc_naive()
    emitted = 0
    new_dar_ids: list[str] = []

    for col in cols:
        finding = _analyze_column(conn, table, col)
        if finding is None:
            continue
        # Stage B — non-LLM DAR type; schema uniformity per §4.3b.
        finding["blockers_addressed"] = []
        dar_id = _next_dar_id()
        row = {
            "id": dar_id,
            "analysis_type": _ANALYSIS_TYPE,
            "executed_at_utc": _iso(executed),
            "result_json": json.dumps(finding, separators=(",", ":"), default=str),
            "promoted": "false",
            "promoted_at_utc": "",
            "promoted_to_target_id": "",
            "run_id": run_id,
            "query_sql": f"-- segmentation quartiles on raw_sap.{table}.{col}",
            "row_count": "",
            "error_message": "",
            "status": "success",
            "superseded_by": "",
            "executed_by": "run_segmentation_analysis.py",
            "schema_version": schema_hash,
            "source_tables": table,
            "domain_name": _DOMAIN_NAME,
            "last_source_ingestion_at": "",
        }
        _append_dar(row)
        new_dar_ids.append(dar_id)
        print(f"  [ok] {dar_id} segmentation_threshold {table}.{col}: "
              f"thresholds={finding['thresholds']}")
        emitted += 1
    # known_issue #76: if we iterated `cols` but every numeric column was
    # individually skipped (constant or all-null), no DAR was emitted —
    # downstream readers (e.g., term_eda_prereq) would see "analyzer
    # missing" instead of "analyzer ran, no meaningful output". Emit a
    # skipped DAR per Stage D.1 §4.3b so the analyzer-ran signal surfaces.
    if emitted == 0:
        from _skipped_dar import build_skipped_dar_row  # noqa: E402
        skipped = build_skipped_dar_row(
            dar_id=_next_dar_id(),
            analysis_type=_ANALYSIS_TYPE,
            source_tables=table,
            skip_reason=(
                f"all {len(cols)} numeric column(s) constant or all-null"
            ),
            schema_version=schema_hash,
            last_source_ingestion_at="",
            executed_by="run_segmentation_analysis.py",
            domain_name=_DOMAIN_NAME,
        )
        _append_dar(skipped)
        supersede_prior_dars_for_table(
            _ANALYSIS_TYPE, table, [skipped["id"]],
        )
        print(f"  [ok] {skipped['id']} segmentation_threshold {table}: "
              f"SKIPPED (all columns constant or all-null)")
        return 1
    if new_dar_ids:
        supersede_prior_dars_for_table(
            _ANALYSIS_TYPE, table, new_dar_ids,
        )
    return emitted


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="segmentation_threshold analyzer.")
    p.add_argument("--tables", type=str, default=None,
                   help="Comma-separated raw table names. Default: all.")
    p.add_argument("--no-parquet-sync", action="store_true",
                   help="Skip post-write parquet export + Streamlit view invalidation (for batch callers)")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None, conn=None) -> int:
    args = _parse_args(argv)
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB_PATH), read_only=True)
    total = 0
    try:
        if args.tables:
            tables = [t.strip().lower() for t in args.tables.split(",") if t.strip()]
        else:
            tables = _raw_sap_tables(conn)
        print(f"Analyzing {len(tables)} raw_sap tables for segmentation_threshold...")
        for t in tables:
            total += analyze_table(conn, t)
    finally:
        if owned:
            conn.close()
    print(f"\nTotal segmentation_threshold DARs emitted: {total}")
    if total > 0:
        # known_issue #53 — see scripts/_parquet_sync.py module docstring.
        from _parquet_sync import sync_parquet_and_invalidate
        sync_parquet_and_invalidate(
            project_root=_PROJECT_ROOT,
            seed_name="domain_analysis_results",
            skip=args.no_parquet_sync,
            source="run_segmentation_analysis",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
