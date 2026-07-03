"""temporal_coverage analyzer.

Emits `domain_analysis_results` rows with analysis_type='temporal_coverage'
for each date/timestamp column in each raw source table. One DAR row per
(table, column) combination. Feeds Layer A's `temporal_coverage_json`
field via compile_semantic_model.py co-compilation.

CLI:
  python scripts/run_date_analysis.py                        # all raw tables
  python scripts/run_date_analysis.py --tables ekpo,ekko     # subset

Deterministic — no LLM calls. Pure SQL aggregation per column:
  - MIN, MAX (observed bounds)
  - span_days = MAX - MIN
  - null_pct = COUNT null / COUNT *
  - gap_count = distinct_months_observed compared against
    expected_months (MAX - MIN spans); approximates continuity

Skips tables without date columns. Skips columns with all-null (span
indeterminate). Logged.

Exit codes:
  0 — success (any number of DARs emitted, including zero)
  1 — DuckDB error
  2 — write safeguard refused
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
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

_ANALYSIS_TYPE = "temporal_coverage"
_DOMAIN_NAME = "temporal"

# DuckDB type names that qualify as date/time columns
_DATE_TYPES = frozenset({
    "DATE", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ",
    "TIME", "TIMESTAMP_S", "TIMESTAMP_MS", "TIMESTAMP_NS",
})

# SAP convention: raw dates exported as VARCHAR in YYYYMMDD form. Column
# names end with DAT / DATE / _DATE or contain common date tokens.
# Enumerate typical suffixes (exact or word-boundary match).
_SAP_DATE_NAME_SUFFIXES = (
    "DAT", "DATE", "_DATE", "DATUM",
    "BUDAT", "BEDAT", "BLDAT", "ERDAT", "AEDAT", "BADAT",
    "RSDAT", "LADAT", "KDATB", "KDATE", "SESSION_DATE",
)

# SAP *DT-suffix convention: 2-4 uppercase letters + DT.
# Catches EINDT, BEGDT, INBDT, FRGDT. Known non-date exclusions below.
_SAP_DT_SUFFIX_PATTERN = re.compile(r"^[A-Z]{2,4}DT$")
# SAP control fields that match *DT but aren't dates.
# MANDT = client code (always '100' in single-client systems).
_SAP_DT_BLOCKLIST = frozenset({"MANDT"})


def _now_utc_naive() -> dt.datetime:
    """RULE 36 / anti-pattern #54."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _iso(value) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%f")
    if isinstance(value, dt.date):
        return value.strftime("%Y-%m-%d")
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


def _is_sap_date_column_name(name: str) -> bool:
    """True when column name matches SAP raw-date naming convention
    (VARCHAR(8) in YYYYMMDD form). Stricter than plain substring to
    avoid false positives like MANDT (client, not a date)."""
    up = name.upper()
    # Canonical SAP date column names (exact match)
    if up in {"BUDAT", "BEDAT", "BLDAT", "ERDAT", "AEDAT", "BADAT",
             "RSDAT", "LADAT", "KDATB", "KDATE", "DATUM",
             "ANESSION_DATE", "SESSION_DATE"}:
        return True
    # Suffix heuristic: ends with DAT / DATE / DATUM
    for suffix in ("DATE", "DATUM"):
        if up.endswith(suffix):
            return True
    # "DAT" suffix is tricky — MANDT ends in DT but not DAT. Require DAT
    # to come after at least one alpha char and not be preceded by a
    # consonant that makes it non-date (MANDT is caught by being !=DAT-suffix).
    if up.endswith("DAT") and not up.endswith("NDAT"):
        return True
    # *DT suffix (EINDT, BEGDT, INBDT, FRGDT) — exclude MANDT-class control fields.
    if up not in _SAP_DT_BLOCKLIST and _SAP_DT_SUFFIX_PATTERN.match(up):
        return True
    return False


def _date_columns(conn, table: str) -> list[tuple[str, str]]:
    """Returns list of (col_name, detect_mode) where detect_mode is
    'date_type' for native DATE/TIMESTAMP, 'sap_varchar' for
    SAP-convention VARCHAR(YYYYMMDD)."""
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    cols: list[tuple[str, str]] = []
    for name, dt_name in rows:
        if not dt_name:
            continue
        up = dt_name.upper()
        if up in _DATE_TYPES or up.startswith("TIMESTAMP") or up == "DATE":
            cols.append((name, "date_type"))
        elif up == "VARCHAR" and _is_sap_date_column_name(name):
            cols.append((name, "sap_varchar"))
    return cols


def _analyze_column(conn, table: str, col: str, detect_mode: str = "date_type") -> Optional[dict]:
    """Returns finding dict or None if column is all-null / unanalyzable.

    detect_mode='date_type': column is native DATE/TIMESTAMP — SQL uses
        column directly.
    detect_mode='sap_varchar': column is VARCHAR holding YYYYMMDD —
        SQL wraps in strptime() to parse. Empty-string and null both
        treated as missing; invalid formats return NULL from strptime.
    """
    safe_col = f'"{col}"'
    safe_table = f'raw_sap."{table}"'
    # Expression representing the column as a TIMESTAMP
    if detect_mode == "sap_varchar":
        # strptime returns NULL on parse failure; NULLIF handles empty string
        col_expr = f"strptime(NULLIF(CAST({safe_col} AS VARCHAR), ''), '%Y%m%d')"
    else:
        col_expr = f"CAST({safe_col} AS TIMESTAMP)"
    try:
        row = conn.execute(
            f"SELECT COUNT(*) AS n, "
            f"       COUNT({col_expr}) AS non_null, "
            f"       MIN({col_expr}) AS mn, "
            f"       MAX({col_expr}) AS mx, "
            f"       COUNT(DISTINCT DATE_TRUNC('month', {col_expr})) AS distinct_months "
            f"FROM {safe_table}"
        ).fetchone()
    except duckdb.Error as e:
        print(f"    [skip] {table}.{col}: {e}")
        return None

    n, non_null, mn, mx, distinct_months = row
    if not n:
        return None
    null_pct = round((n - non_null) / n, 6) if n else 1.0
    if non_null == 0 or mn is None or mx is None:
        return {
            "col_name": col,
            "min": None,
            "max": None,
            "span_days": None,
            "null_pct": null_pct,
            "gap_count": None,
            "note": "all-null column; no observable range",
        }
    # Span
    try:
        span_days = (mx - mn).days if hasattr(mx - mn, "days") else int((mx - mn).total_seconds() / 86400)
    except Exception:
        span_days = None
    # Gap count: expected distinct months ≈ ceil(span_days / 30); detected
    # minus observed is the gap approximation. Clamp at 0.
    if span_days is None or distinct_months is None:
        gap_count = None
    else:
        expected_months = max(1, (span_days // 30) + 1)
        gap_count = max(0, expected_months - int(distinct_months))

    return {
        "col_name": col,
        "detect_mode": detect_mode,
        "min": _iso(mn),
        "max": _iso(mx),
        "span_days": span_days,
        "null_pct": null_pct,
        "gap_count": gap_count,
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
            # Fill missing keys with '' so DictWriter doesn't raise
            w.writerow({k: r.get(k, "") for k in _DAR_FIELDS})
    os.replace(tmp, _DAR_CSV)


def analyze_table(conn, table: str) -> int:
    """Analyze one raw table; emit one DAR per date column. Returns count emitted.

    Stage D.1: when no date/timestamp columns exist, emit a canonical
    skipped DAR instead of silently returning — Stage C prereq counts
    skipped DARs as satisfying the 'date' analyzer requirement.
    """
    cols = _date_columns(conn, table)
    if not cols:
        print(f"  [skip] {table}: no date/timestamp columns")
        from _skipped_dar import build_skipped_dar_row  # noqa: E402
        skipped = build_skipped_dar_row(
            dar_id=_next_dar_id(),
            analysis_type=_ANALYSIS_TYPE,
            source_tables=table,
            skip_reason="no date/timestamp columns in table",
            schema_version=_schema_version(conn, table),
            last_source_ingestion_at="",
            executed_by="run_date_analysis.py",
            domain_name=_DOMAIN_NAME,
        )
        _append_dar(skipped)
        supersede_prior_dars_for_table(
            skipped["analysis_type"], skipped["source_tables"],
            [skipped["id"]],
        )
        print(f"  [ok] {skipped['id']} temporal_coverage {table}: SKIPPED")
        return 1

    run_id = f"date_analysis_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{table}"
    schema_hash = _schema_version(conn, table)
    executed = _now_utc_naive()
    emitted = 0
    new_dar_ids: list[str] = []

    for col, detect_mode in cols:
        finding = _analyze_column(conn, table, col, detect_mode)
        if finding is None:
            continue
        # Stage B — non-LLM DAR type; keep the result schema uniform
        # with the LLM analyzers.
        # No LLM contract applies, so blockers_contract_violation is omitted.
        finding["blockers_addressed"] = []
        dar_id = _next_dar_id()
        row = {
            "id": dar_id,
            "analysis_type": _ANALYSIS_TYPE,
            "executed_at_utc": _iso(executed),
            "result_json": json.dumps(finding, default=str),
            "promoted": "false",
            "promoted_at_utc": "",
            "promoted_to_target_id": "",
            "run_id": run_id,
            "query_sql": f"-- date-range aggregation on raw_sap.{table}.{col}",
            "row_count": "",
            "error_message": "",
            "status": "success",
            "superseded_by": "",
            "executed_by": "run_date_analysis.py",
            "schema_version": schema_hash,
            "source_tables": table,
            "domain_name": _DOMAIN_NAME,
            "last_source_ingestion_at": "",
        }
        _append_dar(row)
        new_dar_ids.append(dar_id)
        print(f"  [ok] {dar_id} temporal_coverage {table}.{col}: "
              f"min={finding.get('min')} max={finding.get('max')} "
              f"span={finding.get('span_days')}d null_pct={finding.get('null_pct')} "
              f"gaps={finding.get('gap_count')}")
        emitted += 1
    if new_dar_ids:
        supersede_prior_dars_for_table(
            _ANALYSIS_TYPE, table, new_dar_ids,
        )
    return emitted


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Temporal-coverage analyzer.")
    p.add_argument("--tables", type=str, default=None,
                   help="Comma-separated raw table names. Default: all raw tables.")
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
        print(f"Analyzing {len(tables)} raw_sap tables for temporal_coverage...")
        for t in tables:
            total += analyze_table(conn, t)
    finally:
        if owned:
            conn.close()
    print(f"\nTotal temporal_coverage DARs emitted: {total}")
    if total > 0:
        # known_issue #53 — see scripts/_parquet_sync.py module docstring.
        from _parquet_sync import sync_parquet_and_invalidate
        sync_parquet_and_invalidate(
            project_root=_PROJECT_ROOT,
            seed_name="domain_analysis_results",
            skip=args.no_parquet_sync,
            source="run_date_analysis",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
