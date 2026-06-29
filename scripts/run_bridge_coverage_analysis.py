"""Option B Phase 1 — bridge_coverage_by_filter analyzer.

Per FK pair from schema_discovery DARs (per-table) and per filter
column from the Phase-1 allowlist that exists in the FK's to-table,
measure reachability: which values of the filter column appear in
rows reachable through the FK join? Emit one DAR per (FK pair,
filter column).

Closes the LLM-overconfident-yes architectural gap (known_issue #100):
the runtime gate (Phase 2) consumes these DARs to refuse SQL that
filters on values empirically unreachable through the chosen FK.

CLI:
  python scripts/run_bridge_coverage_analysis.py
  python scripts/run_bridge_coverage_analysis.py --scope equi,mkpf,mseg

Per design doc tasks/option_b_design.md Component 1, refinements F-1
(FK-pair vs 2-hop bridge_tables) and F-4 (analyzer measures raw_sap.*).

Spec: tasks/option_b_design.md.
Parent issue: known_issue #100.

Exit codes:
  0 — success (>=0 DARs emitted)
  1 — argument or DuckDB error
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
_DB_PATH = _PROJECT_ROOT / "cpe_analytics.duckdb"
_DAR_CSV = _SEED_DIR / "domain_analysis_results.csv"

sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

_DAR_FIELDS: list[str] = [
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name",
    "last_source_ingestion_at",
]

_ANALYSIS_TYPE = "bridge_coverage_by_filter"
_DOMAIN_NAME = "structural"

# Per design doc Component 1 OQ-D: Phase 1 hand-curated low-cardinality
# SAP code-table columns. Phase 2+ extends via code_tables-DAR derivation.
_ALLOWLIST_FILTER_COLUMNS: tuple[str, ...] = (
    "BWART", "BSTYP", "BSART", "BLART", "MTART",
    "KTOPL", "LOEKZ", "STATU", "KOART", "SHKZG",
)

# Per design doc Component 1 "Bounding": skip filter columns whose
# distinct value count exceeds this; emit DAR with status='skipped'.
_CARDINALITY_BOUND = 1000


# --- timestamp + id helpers --------------------------------------------

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


# --- schema introspection ---------------------------------------------

def _table_columns(conn, table: str) -> dict[str, str]:
    """{original_case_column_name: data_type_uppercase} for raw_sap.<table>."""
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?)",
        [table],
    ).fetchall()
    return {c: (t or "").upper() for c, t in rows}


def _resolve_case_keys(t_cols: dict[str, str],
                       upper_keys: list[str]) -> Optional[list[str]]:
    """Map upper-cased key names to original column case for safe SQL.
    Returns None if any key is missing (defensive against schema drift)."""
    up_to_orig = {c.upper(): c for c in t_cols}
    out: list[str] = []
    for k in upper_keys:
        if k.upper() not in up_to_orig:
            return None
        out.append(up_to_orig[k.upper()])
    return out


def _schema_version_pair(conn, t_from: str, t_to: str) -> str:
    rows = conn.execute(
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema='raw_sap' "
        "  AND LOWER(table_name) IN (LOWER(?), LOWER(?)) "
        "ORDER BY table_name, ordinal_position",
        [t_from, t_to],
    ).fetchall()
    payload = ",".join(f"{tn}.{c}:{t}" for tn, c, t in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# --- input loading ----------------------------------------------------

def _load_in_scope_fk_candidates(
    conn, scope_tables: list[str],
) -> list[dict]:
    """Read latest successful schema_discovery DAR for each scope table;
    extract high-confidence FK candidates whose to_table is also in scope.

    Returns list of dicts:
        {from_table, from_columns, to_table, to_columns,
         referential_integrity_pct, confidence, schema_discovery_dar_id}
    """
    scope_lc = {t.lower() for t in scope_tables}
    out: list[dict] = []
    for tbl in scope_tables:
        row = conn.execute(
            "SELECT id, result_json FROM main_seeds.domain_analysis_results "
            "WHERE analysis_type = 'schema_discovery' "
            "  AND status = 'success' "
            "  AND LOWER(source_tables) = LOWER(?) "
            "ORDER BY executed_at_utc DESC LIMIT 1",
            [tbl],
        ).fetchone()
        if not row:
            continue
        try:
            payload = json.loads(row[1] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        sd_dar_id = row[0]
        for fk in payload.get("fk_candidates") or []:
            if fk.get("confidence") != "high":
                continue
            to_table = (fk.get("to_table") or "").lower()
            if to_table not in scope_lc:
                continue
            from_columns = list(fk.get("from_columns") or [])
            to_columns = list(fk.get("to_columns") or [])
            if not from_columns or not to_columns:
                continue
            if len(from_columns) != len(to_columns):
                continue
            out.append({
                "from_table": tbl.lower(),
                "from_columns": [c.upper() for c in from_columns],
                "to_table": to_table,
                "to_columns": [c.upper() for c in to_columns],
                "referential_integrity_pct": fk.get("referential_integrity_pct"),
                "confidence": fk.get("confidence"),
                "schema_discovery_dar_id": sd_dar_id,
            })
    return out


# --- reachability measurement -----------------------------------------

def _measure_reachability(
    conn, fk: dict, filter_column: str,
    to_table_columns: dict[str, str],
) -> Optional[dict]:
    """Run GROUP BY through the FK + SELECT DISTINCT on the to-table.

    Returns dict with reachable_values, all_distinct, cardinality_overflow,
    evidence_query_sql. None on FK key resolution failure; dict with
    'error' on DuckDB error.
    """
    from_cols_orig = _resolve_case_keys(
        _table_columns(conn, fk["from_table"]), fk["from_columns"]
    )
    to_cols_orig = _resolve_case_keys(to_table_columns, fk["to_columns"])
    fcol_orig = next(
        (c for c in to_table_columns if c.upper() == filter_column.upper()),
        None,
    )
    if from_cols_orig is None or to_cols_orig is None or fcol_orig is None:
        return None

    safe_from = f'raw_sap."{fk["from_table"]}"'
    safe_to = f'raw_sap."{fk["to_table"]}"'
    join_pred = " AND ".join(
        f'f."{a}" = t."{b}"'
        for a, b in zip(from_cols_orig, to_cols_orig)
    )
    group_sql = (
        f'SELECT t."{fcol_orig}" AS value, COUNT(*) AS row_count_via_bridge '
        f'FROM {safe_from} f '
        f'JOIN {safe_to} t ON {join_pred} '
        f'WHERE t."{fcol_orig}" IS NOT NULL '
        f'GROUP BY 1 ORDER BY 2 DESC'
    )
    distinct_sql = (
        f'SELECT DISTINCT "{fcol_orig}" '
        f'FROM {safe_to} '
        f'WHERE "{fcol_orig}" IS NOT NULL '
        f'LIMIT {_CARDINALITY_BOUND + 1}'
    )
    try:
        group_rows = conn.execute(group_sql).fetchall()
        distinct_rows = conn.execute(distinct_sql).fetchall()
    except duckdb.Error as e:
        return {"error": str(e), "evidence_query_sql": group_sql}

    reachable_values = [
        {"value": "" if r[0] is None else str(r[0]),
         "row_count_via_bridge": int(r[1] or 0)}
        for r in group_rows
    ]
    distinct_raw = [
        ("" if r[0] is None else str(r[0])) for r in distinct_rows
    ]
    cardinality_overflow = len(distinct_raw) > _CARDINALITY_BOUND
    all_distinct = sorted(set(distinct_raw[:_CARDINALITY_BOUND]))
    return {
        "reachable_values": reachable_values,
        "all_distinct": all_distinct,
        "cardinality_overflow": cardinality_overflow,
        "evidence_query_sql": group_sql,
    }


# --- DAR row construction ---------------------------------------------

def _build_rationale(fk: dict, filter_column: str,
                     n_reach: int, n_unreach: int) -> str:
    keys = "+".join(fk["from_columns"])
    n_total = n_reach + n_unreach
    if n_total == 0:
        return (f"No non-null {filter_column} values present in "
                f"{fk['to_table']}.")
    if n_unreach == 0:
        return (f"All {n_total} {filter_column} values reachable through "
                f"{fk['from_table']}->{fk['to_table']} on {keys}.")
    return (f"{n_reach} of {n_total} {filter_column} values reachable "
            f"through {fk['from_table']}->{fk['to_table']} on {keys}; "
            f"{n_unreach} unreachable. SQL filtering on an unreachable "
            f"value will return 0 rows.")


def _build_dar_row(*, fk: dict, filter_column: str,
                   to_table_columns: dict[str, str],
                   measurement: dict, status: str, run_id: str,
                   schema_version: str) -> dict:
    """Per OQ-A schema in design doc, F-1 refinement applied: FK pair
    structure (via_table=null, via_keys_mid_to_to=[])."""
    fcol_orig = next(
        (c for c in to_table_columns
         if c.upper() == filter_column.upper()),
        filter_column,
    )
    fcol_type = to_table_columns.get(fcol_orig, "")
    bridge_block = {
        "from_table": fk["from_table"],
        "via_table": None,
        "to_table": fk["to_table"],
        "via_keys_from_to_mid": list(fk["from_columns"]),
        "via_keys_mid_to_to": [],
        "from_to_mid_to_columns": list(fk["to_columns"]),
        "schema_discovery_dar_id": fk["schema_discovery_dar_id"],
        "referential_integrity_pct": fk.get("referential_integrity_pct"),
    }
    filter_block = {
        "table": fk["to_table"],
        "column": fcol_orig.upper(),
        "data_type": fcol_type,
    }

    if status == "success":
        reachable = measurement["reachable_values"]
        reachable_set = {r["value"] for r in reachable}
        all_distinct = measurement["all_distinct"]
        unreachable = sorted(set(all_distinct) - reachable_set)
        result_json = {
            "bridge": bridge_block,
            "filter_column": filter_block,
            "reachable_values": reachable,
            "all_distinct_values": all_distinct,
            "unreachable_values": unreachable,
            "value_cardinality": {
                "all_distinct": len(all_distinct),
                "reachable": len(reachable_set),
                "unreachable": len(unreachable),
            },
            "evidence_query_sql": measurement["evidence_query_sql"],
            "measured_at_utc": _iso(_now_utc_naive()),
            "measurement_method": "group_by_through_fk",
            "rationale": _build_rationale(
                fk, filter_block["column"], len(reachable_set),
                len(unreachable),
            ),
        }
        row_count = str(len(reachable))
        error_message = ""
        query_sql = measurement["evidence_query_sql"]
    elif status == "skipped":
        result_json = {
            "bridge": bridge_block,
            "filter_column": filter_block,
            "measurement_method": "group_by_through_fk",
            "skip_reason": measurement.get("skip_reason", ""),
            "rationale": measurement.get("rationale", ""),
        }
        row_count = ""
        error_message = ""
        query_sql = (f"-- skipped: {measurement.get('skip_reason', '')} "
                     f"on {fk['from_table']}->{fk['to_table']} "
                     f"filter {filter_column}")
    else:  # error
        result_json = {
            "bridge": bridge_block,
            "filter_column": filter_block,
            "measurement_method": "group_by_through_fk",
            "error": measurement.get("error", ""),
        }
        row_count = ""
        error_message = measurement.get("error", "")
        query_sql = measurement.get("evidence_query_sql", "")

    source_tables = ",".join(sorted({fk["from_table"], fk["to_table"]}))
    return {
        "id": _next_dar_id(),
        "analysis_type": _ANALYSIS_TYPE,
        "executed_at_utc": _iso(_now_utc_naive()),
        "result_json": json.dumps(result_json, separators=(",", ":")),
        "promoted": "false",
        "promoted_at_utc": "",
        "promoted_to_target_id": "",
        "run_id": run_id,
        "query_sql": query_sql,
        "row_count": row_count,
        "error_message": error_message,
        "status": status,
        "superseded_by": "",
        "executed_by": "run_bridge_coverage_analysis.py",
        "schema_version": schema_version,
        "source_tables": source_tables,
        "domain_name": _DOMAIN_NAME,
        "last_source_ingestion_at": "",
    }


def _append_dar(row: dict) -> None:
    existing: list[dict] = []
    if _DAR_CSV.exists():
        with _DAR_CSV.open("r", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    existing.append(row)
    tmp = _DAR_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_DAR_FIELDS, lineterminator="\n",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        for r in existing:
            w.writerow({k: r.get(k, "") for k in _DAR_FIELDS})
    os.replace(tmp, _DAR_CSV)


# --- analysis driver --------------------------------------------------

def analyze(conn, scope_tables: list[str]) -> int:
    """Enumerate FK candidates × allowlist columns; measure + emit DARs.
    Returns count of DARs emitted (any status)."""
    fk_candidates = _load_in_scope_fk_candidates(conn, scope_tables)
    if not fk_candidates:
        print(
            "[ERROR] bridge_coverage_by_filter analyzer: no in-scope "
            "high-confidence FK candidates found from schema_discovery "
            "DARs. Run schema_discovery first or check scope.",
            file=sys.stderr,
        )
        return 0

    run_id = (f"bridge_coverage_"
              f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    schema_version_cache: dict[tuple[str, str], str] = {}
    emitted = 0

    for fk in fk_candidates:
        to_cols = _table_columns(conn, fk["to_table"])
        ver_key = tuple(sorted([fk["from_table"], fk["to_table"]]))
        if ver_key not in schema_version_cache:
            schema_version_cache[ver_key] = _schema_version_pair(
                conn, fk["from_table"], fk["to_table"]
            )
        schema_version = schema_version_cache[ver_key]

        for filter_column in _ALLOWLIST_FILTER_COLUMNS:
            # Allowlist column must exist in the to_table; otherwise
            # silently skip (no DAR — most pairs are no-ops).
            if not any(c.upper() == filter_column for c in to_cols):
                continue

            measurement = _measure_reachability(
                conn, fk, filter_column, to_cols,
            )
            if measurement is None:
                continue

            if "error" in measurement:
                row = _build_dar_row(
                    fk=fk, filter_column=filter_column,
                    to_table_columns=to_cols, measurement=measurement,
                    status="error", run_id=run_id,
                    schema_version=schema_version,
                )
            elif measurement["cardinality_overflow"]:
                row = _build_dar_row(
                    fk=fk, filter_column=filter_column,
                    to_table_columns=to_cols,
                    measurement={
                        **measurement,
                        "skip_reason": "high_cardinality",
                        "rationale": (
                            f"{filter_column} has > {_CARDINALITY_BOUND} "
                            f"distinct values in {fk['to_table']}; "
                            f"reachability not measured."
                        ),
                    },
                    status="skipped", run_id=run_id,
                    schema_version=schema_version,
                )
            else:
                row = _build_dar_row(
                    fk=fk, filter_column=filter_column,
                    to_table_columns=to_cols, measurement=measurement,
                    status="success", run_id=run_id,
                    schema_version=schema_version,
                )

            _append_dar(row)
            emitted += 1
            keys = "+".join(fk["from_columns"])
            print(
                f"  [ok] {row['id']} {fk['from_table']}->{fk['to_table']} "
                f"on {keys} filter {filter_column} -> {row['status']}"
            )

    return emitted


# --- CLI --------------------------------------------------------------

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="bridge_coverage_by_filter analyzer."
    )
    p.add_argument(
        "--scope", type=str, default=None,
        help=("Comma-separated raw_sap tables. Default: all tables that "
              "have a successful schema_discovery DAR."),
    )
    p.add_argument(
        "--no-parquet-sync", action="store_true",
        help="Skip post-write parquet export (for batch callers).",
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None,
         conn: Optional[duckdb.DuckDBPyConnection] = None) -> int:
    args = _parse_args(argv)
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB_PATH), read_only=True)
    emitted = 0
    try:
        if args.scope:
            scope = [t.strip().lower() for t in args.scope.split(",")
                     if t.strip()]
        else:
            rows = conn.execute(
                "SELECT DISTINCT LOWER(source_tables) "
                "FROM main_seeds.domain_analysis_results "
                "WHERE analysis_type = 'schema_discovery' "
                "  AND status = 'success'"
            ).fetchall()
            scope = sorted({r[0] for r in rows if r[0]})
        if not scope:
            print("ERROR: no scope tables; provide --scope or run "
                  "schema_discovery first.", file=sys.stderr)
            return 1
        print(f"Analyzing bridge_coverage for scope: {scope}")
        emitted = analyze(conn, scope)
        print(
            f"\nTotal bridge_coverage_by_filter DARs emitted: {emitted}"
        )
    finally:
        if owned:
            conn.close()
    if emitted > 0:
        try:
            from _parquet_sync import sync_parquet_and_invalidate
            sync_parquet_and_invalidate(
                project_root=_PROJECT_ROOT,
                seed_name="domain_analysis_results",
                skip=args.no_parquet_sync,
                source="run_bridge_coverage_analysis",
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] parquet sync skipped: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
