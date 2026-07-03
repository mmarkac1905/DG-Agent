"""grain_relationship analyzer.

Emits `domain_analysis_results` rows with analysis_type='grain_relationship'
for detected header/detail sum-match relationships between pairs of raw
source tables. Feeds Layer A's grain_relationships_json via
compile_semantic_model.py co-compilation. Each detected relationship is
written as symmetric entries (one per direction).

Deterministic — no LLM calls. Pure SQL aggregation + heuristic matching:
  - Pre-filter pairs (T1 < T2 lexicographic; ≥1 shared numeric column)
  - For each shared numeric column, compute SUM(T1.col) GROUP BY join_key
    and compare to T2.col sums on the same join_key
  - sum_match_pct = fraction of joined keys where ABS diff / header < 0.01
  - Confidence: >0.99 high, 0.90-0.99 medium, <0.90 low (suppressed)

Pair scope format: source_tables stored comma-separated "t1,t2",
lexicographically sorted.

CLI:
  python scripts/run_grain_relationship_analysis.py                     # all raw pairs
  python scripts/run_grain_relationship_analysis.py --pairs ekko,ekpo   # single pair

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

_ANALYSIS_TYPE = "grain_relationship"
_DOMAIN_NAME = "grain"
_TOLERANCE = 0.01  # ±1% sum-match tolerance
_CONF_HIGH = 0.99
_CONF_MEDIUM = 0.90

_NUMERIC_TYPES = frozenset({
    "DECIMAL", "DOUBLE", "INTEGER", "BIGINT", "FLOAT", "REAL",
    "NUMERIC", "HUGEINT", "SMALLINT", "TINYINT",
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


def _schema_version_pair(conn, t1: str, t2: str) -> str:
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' "
        "AND LOWER(table_name) IN (LOWER(?), LOWER(?)) "
        "ORDER BY table_name, ordinal_position",
        [t1, t2],
    ).fetchall()
    payload = ",".join(f"{tn}.{c}:{t}" for tn, c, t in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _table_columns(conn, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='raw_sap' AND LOWER(table_name)=LOWER(?)",
        [table],
    ).fetchall()
    return {c: (t or "") for c, t in rows}


def _raw_sap_tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='raw_sap' ORDER BY table_name"
    ).fetchall()
    return [r[0].lower() for r in rows]


def _numeric_column_names(cols: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for name, ty in cols.items():
        up = (ty or "").upper()
        if up in _NUMERIC_TYPES or up.startswith("DECIMAL"):
            out.add(name.upper())
    return out


def _find_shared_join_key(t1_cols: dict[str, str], t2_cols: dict[str, str]) -> Optional[str]:
    """Heuristic: shared column name with same upper-case form is the
    join key. SAP key columns typically uppercase (EBELN, MATNR, etc.).
    Pick the LONGEST shared-name column to favor real keys over generic
    fields like MANDT.
    """
    t1_up = {c.upper(): c for c in t1_cols}
    t2_up = {c.upper(): c for c in t2_cols}
    shared = sorted(
        set(t1_up) & set(t2_up),
        key=lambda x: (-len(x), x),
    )
    # Skip common non-key fields
    blacklist = {"MANDT", "ERNAM", "ERDAT", "UZEIT", "ERZEIT", "LOEKZ"}
    for k in shared:
        if k in blacklist:
            continue
        return k
    return None


def _compute_sum_match(
    conn,
    t1: str, t1_join: str, t1_col: str,
    t2: str, t2_join: str, t2_col: str,
) -> Optional[float]:
    """Returns sum_match_pct: fraction of joined keys where detail-sum
    matches header-sum within ±_TOLERANCE. Returns None if no matching
    keys (insufficient signal).
    """
    safe_t1 = f'raw_sap."{t1}"'
    safe_t2 = f'raw_sap."{t2}"'
    safe_t1_join = f'"{t1_join}"'
    safe_t1_col = f'"{t1_col}"'
    safe_t2_join = f'"{t2_join}"'
    safe_t2_col = f'"{t2_col}"'
    try:
        sql = f"""
            WITH det AS (
                SELECT {safe_t1_join} AS k, SUM({safe_t1_col}) AS s
                FROM {safe_t1}
                WHERE {safe_t1_col} IS NOT NULL
                GROUP BY {safe_t1_join}
            ),
            hdr AS (
                SELECT {safe_t2_join} AS k, SUM({safe_t2_col}) AS s
                FROM {safe_t2}
                WHERE {safe_t2_col} IS NOT NULL
                GROUP BY {safe_t2_join}
            ),
            joined AS (
                SELECT det.k,
                       det.s AS det_sum,
                       hdr.s AS hdr_sum,
                       CASE WHEN hdr.s IS NOT NULL AND hdr.s <> 0
                            THEN ABS(det.s - hdr.s) / NULLIF(ABS(hdr.s), 0)
                            ELSE NULL
                       END AS rel_diff
                  FROM det JOIN hdr ON det.k = hdr.k
            )
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN rel_diff IS NOT NULL AND rel_diff < {_TOLERANCE} THEN 1 ELSE 0 END) AS matching
              FROM joined
        """
        r = conn.execute(sql).fetchone()
    except duckdb.Error:
        return None
    total, matching = r
    if not total:
        return None
    return float(matching) / float(total)


def _emit_symmetric_pair(
    conn,
    t_header: str, header_col: str,
    t_detail: str, detail_col: str,
    sum_match_pct: float,
    confidence: str,
    schema_version: str,
) -> None:
    """Emit TWO DARs: one scoping header-role, one detail-role, so
    each Layer A row gets a self-contained entry with
    other_table + role. Layer A compile reads both and writes matching
    symmetric entries into each table's row.
    """
    run_id = f"grain_relationship_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    executed = _now_utc_naive()
    base = {
        "executed_at_utc": _iso(executed),
        "promoted": "false",
        "promoted_at_utc": "",
        "promoted_to_target_id": "",
        "run_id": run_id,
        "query_sql": (
            f"-- grain_relationship sum_match on {t_detail}.{detail_col} "
            f"vs {t_header}.{header_col}"
        ),
        "row_count": "",
        "error_message": "",
        "status": "success",
        "superseded_by": "",
        "executed_by": "run_grain_relationship_analysis.py",
        "schema_version": schema_version,
        # Source_tables format: comma-separated, lex-sorted
        "source_tables": ",".join(sorted([t_header.lower(), t_detail.lower()])),
        "domain_name": _DOMAIN_NAME,
        "last_source_ingestion_at": "",
        "analysis_type": _ANALYSIS_TYPE,
    }

    # Entry for the HEADER table's Layer A row.
    header_finding = {
        "other_table": t_detail.lower(),
        "role": "header",
        "detail_col": detail_col,
        "header_col": header_col,
        "sum_match_pct": round(sum_match_pct, 4),
        "confidence": confidence,
        "subject_table": t_header.lower(),
        # Stage B — non-LLM DAR type; kept schema-uniform with LLM analyzers.
        "blockers_addressed": [],
    }
    dar_header = dict(base)
    dar_header["id"] = _next_dar_id()
    dar_header["result_json"] = json.dumps(header_finding, separators=(",", ":"))
    _append_dar(dar_header)
    print(f"  [ok] {dar_header['id']} grain_relationship "
          f"{t_header}.{header_col} (header) <-> {t_detail}.{detail_col} (detail) "
          f"match={sum_match_pct:.3f} conf={confidence}")

    # Entry for the DETAIL table's Layer A row (mirror).
    detail_finding = {
        "other_table": t_header.lower(),
        "role": "detail",
        "detail_col": detail_col,
        "header_col": header_col,
        "sum_match_pct": round(sum_match_pct, 4),
        "confidence": confidence,
        "subject_table": t_detail.lower(),
        # Stage B — non-LLM DAR type; kept schema-uniform with LLM analyzers.
        "blockers_addressed": [],
    }
    dar_detail = dict(base)
    dar_detail["id"] = _next_dar_id()
    dar_detail["result_json"] = json.dumps(detail_finding, separators=(",", ":"))
    _append_dar(dar_detail)


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


def _emit_skipped_pair_dar(conn, t1: str, t2: str, skip_reason: str) -> int:
    """Stage D.1: emit a canonical skipped DAR for a pair that cannot be
    analyzed. Uses sorted lowercase comma-joined pair for source_tables
    so Stage C prereq's exact-match check succeeds.
    """
    from _skipped_dar import build_skipped_dar_row  # noqa: E402
    pair_sorted = ",".join(sorted([t1.lower(), t2.lower()]))
    schema_version = _schema_version_pair(conn, t1, t2)
    skipped = build_skipped_dar_row(
        dar_id=_next_dar_id(),
        analysis_type=_ANALYSIS_TYPE,
        source_tables=pair_sorted,
        skip_reason=skip_reason,
        schema_version=schema_version,
        last_source_ingestion_at="",
        executed_by="run_grain_relationship_analysis.py",
        domain_name=_DOMAIN_NAME,
    )
    _append_dar(skipped)
    print(f"  [ok] {skipped['id']} grain_relationship {pair_sorted}: "
          f"SKIPPED ({skip_reason})")
    return 1


def analyze_pair(conn, t1: str, t2: str) -> int:
    """Analyze one table pair. t1 < t2 lexicographically. Tries each
    numeric column shared by name between the two tables. Returns
    count of DARs emitted (0, 2, 4, ... — always even because symmetric).

    Stage D.1: if no joinable relationship exists (no shared numeric
    columns or no shared join key), emit a canonical skipped DAR for
    the pair so Stage C prereq counts the pair as covered.
    """
    t1_cols = _table_columns(conn, t1)
    t2_cols = _table_columns(conn, t2)
    t1_num = _numeric_column_names(t1_cols)
    t2_num = _numeric_column_names(t2_cols)
    shared_numeric_up = t1_num & t2_num
    if not shared_numeric_up:
        return _emit_skipped_pair_dar(
            conn, t1, t2,
            "no shared numeric columns between tables",
        )

    join_key_up = _find_shared_join_key(t1_cols, t2_cols)
    if not join_key_up:
        return _emit_skipped_pair_dar(
            conn, t1, t2,
            "no shared join key between tables",
        )

    # Case-preserve original column names (DuckDB may or may not fold case)
    t1_up_to_orig = {c.upper(): c for c in t1_cols}
    t2_up_to_orig = {c.upper(): c for c in t2_cols}
    join_t1 = t1_up_to_orig[join_key_up]
    join_t2 = t2_up_to_orig[join_key_up]

    schema_version = _schema_version_pair(conn, t1, t2)
    emitted = 0

    for col_up in shared_numeric_up:
        col_t1 = t1_up_to_orig[col_up]
        col_t2 = t2_up_to_orig[col_up]
        # We don't a priori know which is header (t1) or detail (t2).
        # Try both directions; whichever yields SUM(det) ≈ hdr.col is
        # the valid orientation.
        for (t_det, det_col, t_det_join,
             t_hdr, hdr_col, t_hdr_join) in (
            (t1, col_t1, join_t1, t2, col_t2, join_t2),
            (t2, col_t2, join_t2, t1, col_t1, join_t1),
        ):
            pct = _compute_sum_match(
                conn, t_det, t_det_join, det_col,
                t_hdr, t_hdr_join, hdr_col,
            )
            if pct is None:
                continue
            if pct >= _CONF_HIGH:
                conf = "high"
            elif pct >= _CONF_MEDIUM:
                conf = "medium"
            else:
                continue  # low confidence — suppressed by design
            _emit_symmetric_pair(
                conn,
                t_header=t_hdr, header_col=hdr_col,
                t_detail=t_det, detail_col=det_col,
                sum_match_pct=pct, confidence=conf,
                schema_version=schema_version,
            )
            emitted += 2
            break  # one direction matched; skip the reverse

    # Stage D.1: the pair was analyzable (shared numeric + join key exist)
    # but no column/direction produced a high/medium confidence match.
    # Emit a skipped DAR so Stage C prereq treats the pair as covered.
    if emitted == 0:
        return _emit_skipped_pair_dar(
            conn, t1, t2,
            "pair has shared numeric columns and join key, but no "
            "column-direction produced a high/medium sum-match confidence",
        )
    return emitted


def _candidate_pairs(conn, tables: list[str]) -> list[tuple[str, str]]:
    """Pre-filter pairs: lex-ordered, ≥1 shared numeric column."""
    tables = sorted(set(t.lower() for t in tables))
    pairs: list[tuple[str, str]] = []
    col_cache: dict[str, dict[str, str]] = {}
    for t in tables:
        col_cache[t] = _table_columns(conn, t)
    for i, t1 in enumerate(tables):
        for t2 in tables[i + 1:]:
            num1 = _numeric_column_names(col_cache[t1])
            num2 = _numeric_column_names(col_cache[t2])
            if num1 & num2:
                # also need a shared join key
                if _find_shared_join_key(col_cache[t1], col_cache[t2]):
                    pairs.append((t1, t2))
    return pairs


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="grain_relationship analyzer.")
    p.add_argument("--tables", type=str, default=None,
                   help="Comma-separated subset of raw tables to consider. "
                        "Default: all raw tables.")
    p.add_argument("--pairs", type=str, default=None,
                   help="Comma-separated explicit table pair (e.g. ekko,ekpo).")
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
        if args.pairs:
            parts = [t.strip().lower() for t in args.pairs.split(",")]
            if len(parts) != 2:
                print("ERROR: --pairs expects exactly 2 comma-separated table names")
                return 1
            a, b = sorted(parts)
            total += analyze_pair(conn, a, b)
        else:
            if args.tables:
                tables = [t.strip().lower() for t in args.tables.split(",") if t.strip()]
            else:
                tables = _raw_sap_tables(conn)
            pairs = _candidate_pairs(conn, tables)
            print(f"Analyzing {len(pairs)} candidate table pairs for grain_relationship "
                  f"(pre-filtered from {len(tables)} raw tables)...")
            for t1, t2 in pairs:
                total += analyze_pair(conn, t1, t2)
    finally:
        if owned:
            conn.close()
    print(f"\nTotal grain_relationship DARs emitted: {total} "
          f"(always even — symmetric pair entries)")
    if total > 0:
        # known_issue #53 — see scripts/_parquet_sync.py module docstring.
        from _parquet_sync import sync_parquet_and_invalidate
        sync_parquet_and_invalidate(
            project_root=_PROJECT_ROOT,
            seed_name="domain_analysis_results",
            skip=args.no_parquet_sync,
            source="run_grain_relationship_analysis",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
