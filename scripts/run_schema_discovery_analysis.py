"""Stage F — schema_discovery analyzer.

Per-table DAR characterizing the relational structure of a source-schema
table (default raw_sap):
  - PK candidates (single-column + composite, role='key' pruned)
  - FK candidates (with referential integrity % vs all other raw tables)
  - Relationship shapes (1:1 / 1:N / N:M with sum-match for header-detail)
  - Bridge tables (2-hop paths discovered from prior schema_discovery DARs)

Deterministic — no LLM calls. Purpose: replace LLM-recalled schema
knowledge with data-grounded evidence that Stage A scope derivation
can consume.

CLI:
  python scripts/run_schema_discovery_analysis.py --table mseg
  python scripts/run_schema_discovery_analysis.py --table mseg --mode bridges_only

--mode full (default): runs all four steps, emits one new DAR.
--mode bridges_only:   reads existing schema_discovery DAR for the
  target table, re-runs only Step 4 (bridge traversal) against current
  FK graph, emits a fresh DAR and sets superseded_by on the prior row.
  Used by Data Catalog View C's bulk-refresh pass after per-table runs
  complete.

DAR schema — `result_json` structure:
  {
    "pk_candidates":       [{columns, confidence, distinct_ratio, null_count, evidence}],
    "fk_candidates":       [{from_columns, to_table, to_columns,
                             referential_integrity_pct, confidence, evidence}],
    "relationship_shapes": [{pair, via_columns, shape, cardinality,
                             sum_match_pct?, confidence, evidence}],
    "bridge_tables":       [{between, via, path, confidence}],
    "rationale":           "one-paragraph summary",
    "blockers_addressed":  []   // Stage B contract
  }
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import itertools
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import duckdb

from _dar_supersede import supersede_prior_dars_for_table
from _source_config import SOURCE_SCHEMA

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

_ANALYSIS_TYPE = "schema_discovery"
_DOMAIN_NAME = "structural"

# PK discovery guard — Stage 3 (composite) wall-clock ceiling.
_PK_STAGE_3_TIMEOUT_SEC = 120

# FK discovery confidence thresholds (also mirrored in Stage A prompt directive).
_FK_INTEGRITY_HIGH = 0.95
_FK_INTEGRITY_MED = 0.80

# SAP key-column name patterns for heuristic FK discovery fallback.
# Used when source_column_roles has no role='key' entries for a table.
_SAP_KEY_NAMES = frozenset({
    "MANDT", "MATNR", "WERKS", "LIFNR", "KUNNR", "BELNR", "EBELN",
    "VBELN", "EQUNR", "SERNR", "MBLNR", "MJAHR", "BUKRS", "GJAHR",
    "BUDAT", "PERNR", "CHARG", "LGORT", "BWART", "KOSTL", "AUFNR",
    "BEDAT", "TKNUM", "AEDAT", "ERNAM", "BSTMG",
})
_SAP_KEY_SUFFIXES = ("_ID", "_KEY", "_NR", "_NUM")


# ─── Timestamp / ID helpers ────────────────────────────────────────────

def _now_utc_naive() -> dt.datetime:
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
        f"WHERE table_schema='{SOURCE_SCHEMA}' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    payload = ",".join(f"{c}:{t}" for c, t in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _source_tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema='{SOURCE_SCHEMA}' ORDER BY table_name"
    ).fetchall()
    return [r[0].lower() for r in rows]


def _row_count(conn, table: str) -> int:
    try:
        r = conn.execute(f'SELECT COUNT(*) FROM {SOURCE_SCHEMA}."{table}"').fetchone()
        return int(r[0]) if r else 0
    except duckdb.Error:
        return 0


def _table_columns(conn, table: str) -> list[tuple[str, str]]:
    """(column_name, data_type) for a raw_sap table."""
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema='{SOURCE_SCHEMA}' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _key_role_columns(conn, table: str) -> list[str]:
    """Column names with role='key' per source_column_roles for this table.
    Returns [] when no classifications exist."""
    try:
        rows = conn.execute(
            "SELECT column_name FROM main_seeds.source_column_roles "
            "WHERE LOWER(table_name) = LOWER(?) AND role = 'key' "
            "ORDER BY column_name",
            [table],
        ).fetchall()
        return [r[0] for r in rows]
    except duckdb.Error:
        return []


def _heuristic_key_columns(columns: list[tuple[str, str]]) -> list[str]:
    """Fallback: match column names against SAP key-name conventions."""
    out: list[str] = []
    for col, dt_name in columns:
        up = (col or "").upper()
        # Skip pure numeric measure types
        if dt_name and dt_name.upper() in ("DOUBLE", "FLOAT", "REAL", "DECIMAL"):
            # still allow if name strongly suggests key
            if up not in _SAP_KEY_NAMES and not any(up.endswith(s) for s in _SAP_KEY_SUFFIXES):
                continue
        if up in _SAP_KEY_NAMES:
            out.append(col)
            continue
        if any(up.endswith(s) for s in _SAP_KEY_SUFFIXES):
            out.append(col)
    return out


def _candidate_key_columns(conn, table: str) -> list[str]:
    """role='key' first; heuristic fallback when roles are empty."""
    cols = _key_role_columns(conn, table)
    if cols:
        return cols
    return _heuristic_key_columns(_table_columns(conn, table))


# ─── Step 1: PK discovery ──────────────────────────────────────────────

def _col_distinct_and_nulls(conn, table: str, cols: list[str]) -> Optional[tuple[int, int, int]]:
    """Returns (row_count, distinct_count_of_combo, null_in_any_key). None on error."""
    col_list = ", ".join(f'"{c}"' for c in cols)
    null_pred = " OR ".join(f'"{c}" IS NULL' for c in cols)
    try:
        r = conn.execute(
            f'SELECT COUNT(*), COUNT(DISTINCT ({col_list})), '
            f'COUNT(*) FILTER (WHERE {null_pred}) '
            f'FROM {SOURCE_SCHEMA}."{table}"'
        ).fetchone()
        return (int(r[0]), int(r[1]), int(r[2]))
    except duckdb.Error:
        return None


def _discover_pk_candidates(conn, table: str) -> list[dict]:
    """Three-stage short-circuit per spec EDIT S5.

    Stage 1: single-column PK candidates limited to key-role columns.
             If any column passes distinct=row_count AND null=0,
             short-circuit and return that candidate.
    Stage 2: composite keys starting with MANDT (SAP client-dependency).
    Stage 3: up to 4-column composites of key-role columns. Bounded by
             10-column cap + 120s wall-clock.
    """
    key_cols = _candidate_key_columns(conn, table)
    total = _row_count(conn, table)
    if not key_cols or total == 0:
        return []

    candidates: list[dict] = []

    # Stage 1: single-column
    for col in key_cols:
        res = _col_distinct_and_nulls(conn, table, [col])
        if res is None:
            continue
        n, dist, nulls = res
        if n == 0:
            continue
        distinct_ratio = dist / n if n else 0.0
        null_ratio = nulls / n if n else 1.0
        confidence = round(distinct_ratio * (1 - null_ratio), 4)
        if distinct_ratio >= 0.999 and nulls == 0:
            candidates.append({
                "columns": [col],
                "confidence": confidence,
                "distinct_ratio": round(distinct_ratio, 4),
                "null_count": nulls,
                "evidence": (
                    f"count(distinct {col}) = {dist}, row_count = {n}, no nulls"
                ),
            })
            return candidates  # short-circuit — single-column PK found

    # Stage 2: MANDT-prefixed composites
    mandt_col = next((c for c in key_cols if c.upper() == "MANDT"), None)
    non_mandt = [c for c in key_cols if c.upper() != "MANDT"]
    if mandt_col:
        for other in non_mandt:
            res = _col_distinct_and_nulls(conn, table, [mandt_col, other])
            if res is None:
                continue
            n, dist, nulls = res
            if n == 0:
                continue
            distinct_ratio = dist / n
            if distinct_ratio >= 0.999 and nulls == 0:
                candidates.append({
                    "columns": [mandt_col, other],
                    "confidence": round(distinct_ratio, 4),
                    "distinct_ratio": round(distinct_ratio, 4),
                    "null_count": nulls,
                    "evidence": (
                        f"MANDT-prefixed composite: distinct({mandt_col},{other}) = {dist} "
                        f"matches row_count {n}"
                    ),
                })
                return candidates

    # Stage 3: up to 4-column composites, capped + timeout
    if len(non_mandt) > 10:
        # pick top-10 by distinct count to bound combinatorics
        distincts: list[tuple[str, int]] = []
        for col in non_mandt:
            try:
                r = conn.execute(
                    f'SELECT COUNT(DISTINCT "{col}") FROM {SOURCE_SCHEMA}."{table}"'
                ).fetchone()
                distincts.append((col, int(r[0]) if r else 0))
            except duckdb.Error:
                continue
        distincts.sort(key=lambda t: t[1], reverse=True)
        non_mandt = [c for c, _ in distincts[:10]]

    stage3_start = time.perf_counter()
    prefix = [mandt_col] if mandt_col else []
    for k in (2, 3, 4):
        if k == 2 and mandt_col:
            continue  # already tried in Stage 2
        for combo in itertools.combinations(non_mandt, k if not prefix else k - len(prefix)):
            if time.perf_counter() - stage3_start > _PK_STAGE_3_TIMEOUT_SEC:
                # Timeout — return whatever we have
                return candidates
            cols_try = prefix + list(combo)
            res = _col_distinct_and_nulls(conn, table, cols_try)
            if res is None:
                continue
            n, dist, nulls = res
            if n == 0:
                continue
            distinct_ratio = dist / n
            if distinct_ratio >= 0.999 and nulls == 0:
                candidates.append({
                    "columns": cols_try,
                    "confidence": round(distinct_ratio, 4),
                    "distinct_ratio": round(distinct_ratio, 4),
                    "null_count": nulls,
                    "evidence": (
                        f"composite: distinct({','.join(cols_try)}) = {dist} matches "
                        f"row_count {n}"
                    ),
                })
                return candidates  # return first composite hit

    return candidates


# ─── Step 2: FK discovery ──────────────────────────────────────────────

def _types_compatible(t1: str, t2: str) -> bool:
    """Loose DuckDB type compatibility for FK candidacy."""
    if not t1 or not t2:
        return False
    t1u, t2u = t1.upper(), t2.upper()
    if t1u == t2u:
        return True
    char_family = {"VARCHAR", "CHAR", "TEXT", "STRING"}
    int_family = {"INTEGER", "BIGINT", "SMALLINT", "TINYINT", "HUGEINT", "UINTEGER", "UBIGINT"}
    if t1u in char_family and t2u in char_family:
        return True
    if t1u in int_family and t2u in int_family:
        return True
    return False


def _discover_fk_candidates(
    conn, table: str, all_tables: list[str],
) -> list[dict]:
    """For each key-role column in `table`, scan key-role columns in every
    other raw_sap table and compute referential integrity."""
    from_keys = _candidate_key_columns(conn, table)
    if not from_keys:
        return []

    from_cols = {c: t for c, t in _table_columns(conn, table)}
    candidates: list[dict] = []

    for other in all_tables:
        if other == table:
            continue
        to_keys = _candidate_key_columns(conn, other)
        if not to_keys:
            continue
        to_cols = {c: t for c, t in _table_columns(conn, other)}
        for from_col in from_keys:
            from_type = from_cols.get(from_col, "")
            for to_col in to_keys:
                to_type = to_cols.get(to_col, "")
                # Column-name + type match gate (cheap filters first)
                if from_col.upper() != to_col.upper():
                    continue
                if not _types_compatible(from_type, to_type):
                    continue
                # Expensive check: distinct-value overlap
                try:
                    r = conn.execute(
                        f'SELECT '
                        f'  (SELECT COUNT(DISTINCT "{from_col}") FROM {SOURCE_SCHEMA}."{table}" '
                        f'   WHERE "{from_col}" IS NOT NULL) AS from_distinct, '
                        f'  (SELECT COUNT(DISTINCT a."{from_col}") FROM {SOURCE_SCHEMA}."{table}" a '
                        f'   WHERE a."{from_col}" IN (SELECT "{to_col}" FROM {SOURCE_SCHEMA}."{other}") '
                        f'     AND a."{from_col}" IS NOT NULL) AS overlap'
                    ).fetchone()
                except duckdb.Error:
                    continue
                from_distinct = int(r[0]) if r and r[0] is not None else 0
                overlap = int(r[1]) if r and r[1] is not None else 0
                if from_distinct == 0:
                    continue
                integrity = overlap / from_distinct
                if integrity < _FK_INTEGRITY_MED:
                    continue
                confidence_label = (
                    "high" if integrity >= _FK_INTEGRITY_HIGH else "medium"
                )
                candidates.append({
                    "from_columns": [from_col],
                    "to_table": other,
                    "to_columns": [to_col],
                    "referential_integrity_pct": round(integrity * 100, 2),
                    "value_overlap_count": overlap,
                    "confidence": confidence_label,
                    "evidence": (
                        f"{overlap}/{from_distinct} distinct values from "
                        f"{table}.{from_col} exist in {other}.{to_col}"
                    ),
                })
    return candidates


# ─── Step 3: Relationship shape classification ─────────────────────────

def _numeric_columns_in_both(
    conn, t1: str, t2: str,
) -> list[str]:
    """Columns present in both tables with numeric DuckDB type."""
    c1 = {c.upper(): (c, t) for c, t in _table_columns(conn, t1)}
    c2 = {c.upper(): (c, t) for c, t in _table_columns(conn, t2)}
    common = set(c1) & set(c2)
    numeric = {"DOUBLE", "FLOAT", "REAL", "DECIMAL", "INTEGER", "BIGINT",
               "SMALLINT", "HUGEINT", "NUMERIC"}
    out: list[str] = []
    for up in sorted(common):
        t = c1[up][1].upper() if c1[up][1] else ""
        if t in numeric:
            out.append(c1[up][0])
    return out


def _classify_relationship_shapes(
    conn, table: str, fk_candidates: list[dict],
) -> list[dict]:
    """For each high-confidence FK, classify the shape with cardinality
    and (for 1:N) sum-match on any shared numeric column."""
    shapes: list[dict] = []
    for fk in fk_candidates:
        if fk.get("confidence") != "high":
            continue
        to_table = fk["to_table"]
        from_col = fk["from_columns"][0]
        to_col = fk["to_columns"][0]

        try:
            r = conn.execute(
                f'SELECT '
                f'  (SELECT COUNT(*) FROM {SOURCE_SCHEMA}."{table}") AS n_from, '
                f'  (SELECT COUNT(DISTINCT "{from_col}") FROM {SOURCE_SCHEMA}."{table}" '
                f'   WHERE "{from_col}" IS NOT NULL) AS from_distinct, '
                f'  (SELECT COUNT(*) FROM {SOURCE_SCHEMA}."{to_table}") AS n_to, '
                f'  (SELECT COUNT(DISTINCT "{to_col}") FROM {SOURCE_SCHEMA}."{to_table}" '
                f'   WHERE "{to_col}" IS NOT NULL) AS to_distinct'
            ).fetchone()
        except duckdb.Error:
            continue
        n_from, from_dist, n_to, to_dist = r
        if not (n_from and from_dist and n_to and to_dist):
            continue

        from_ratio = n_from / from_dist if from_dist else 0
        to_ratio = n_to / to_dist if to_dist else 0

        # known_issue #133: a ratio threshold (< 1.2) hides genuine 1:N
        # relationships with low average multiplicity (e.g. 3% of orders
        # having multiple payment rows gives ratio 1.045 but is NOT 1:1,
        # and a join on it multiplies those rows). Classify by whether a
        # side actually duplicates its key. Tolerance is relative (0.1%
        # of rows) with an absolute floor of 2 duplicate rows, so a single
        # accidental dupe doesn't flip a genuine 1:1 while small tables
        # still classify correctly.
        def _has_multiplicity(n: int, dist: int) -> bool:
            dup = n - dist
            return dup >= 2 and dup > 0.001 * n

        from_multi = _has_multiplicity(n_from, from_dist)
        to_multi = _has_multiplicity(n_to, to_dist)

        if not from_multi and not to_multi:
            shape, card = "one_to_one", "1:1"
        elif from_multi and not to_multi:
            shape, card = "detail_header", "N:1"
        elif to_multi and not from_multi:
            shape, card = "header_detail", "1:N"
        else:
            shape, card = "many_to_many", "N:M"

        entry: dict = {
            "pair": [table, to_table],
            "via_columns": [from_col],
            "shape": shape,
            "cardinality": card,
            "avg_children_per_parent": round(max(from_ratio, to_ratio), 2),
            "confidence": fk.get("confidence", "high"),
            "evidence": (
                f"{table} has {n_from} rows / {from_dist} distinct {from_col}; "
                f"{to_table} has {n_to} rows / {to_dist} distinct {to_col}. "
                f"Cardinality ratio {round(max(from_ratio, to_ratio), 2)}:1."
            ),
        }

        # Sum-match for header-detail (spec EDIT S7: subsumes
        # grain_relationship's discovery function).
        if shape in ("header_detail", "detail_header"):
            nums = _numeric_columns_in_both(conn, table, to_table)
            if nums:
                header_t, detail_t = (to_table, table) if shape == "detail_header" else (table, to_table)
                num_col = nums[0]
                try:
                    r2 = conn.execute(
                        f'WITH h AS ( '
                        f'  SELECT "{to_col if shape == "header_detail" else from_col}" AS k, '
                        f'         "{num_col}" AS v FROM {SOURCE_SCHEMA}."{header_t}" '
                        f'), d AS ( '
                        f'  SELECT "{from_col if shape == "header_detail" else to_col}" AS k, '
                        f'         SUM("{num_col}") AS s FROM {SOURCE_SCHEMA}."{detail_t}" GROUP BY 1 '
                        f') '
                        f'SELECT '
                        f'  (SELECT COUNT(*) FROM h JOIN d USING(k) '
                        f'   WHERE h.v = d.s) AS matches, '
                        f'  (SELECT COUNT(*) FROM h JOIN d USING(k)) AS total'
                    ).fetchone()
                    if r2 and r2[1]:
                        sum_match_pct = round((r2[0] / r2[1]) * 100, 2)
                        entry["sum_match_pct"] = sum_match_pct
                        entry["sum_match_column"] = num_col
                        entry["evidence"] += (
                            f" Sum-match on {num_col}: {sum_match_pct}% "
                            f"({r2[0]}/{r2[1]} groups)."
                        )
                except duckdb.Error:
                    pass

        shapes.append(entry)
    return shapes


# ─── Step 4: Bridge detection ──────────────────────────────────────────

def _load_existing_fk_graph(conn) -> dict[str, list[dict]]:
    """Read all success schema_discovery DARs + return FK graph keyed by
    source table. Each edge carries from_col → to_table.to_col + confidence."""
    try:
        rows = conn.execute(
            "SELECT source_tables, result_json "
            "FROM main_seeds.domain_analysis_results "
            "WHERE analysis_type = 'schema_discovery' AND status = 'success' "
            "ORDER BY executed_at_utc DESC"
        ).fetchall()
    except duckdb.Error:
        return {}
    graph: dict[str, list[dict]] = {}
    seen_tables: set = set()
    for src, rj in rows:
        if src in seen_tables:
            continue  # take only the latest DAR per table
        seen_tables.add(src)
        try:
            payload = json.loads(rj) if rj else {}
        except json.JSONDecodeError:
            continue
        for fk in payload.get("fk_candidates") or []:
            if fk.get("confidence") != "high":
                continue
            graph.setdefault(src, []).append({
                "from_col": (fk.get("from_columns") or [""])[0],
                "to_table": fk.get("to_table", ""),
                "to_col": (fk.get("to_columns") or [""])[0],
            })
    return graph


def _discover_bridges(
    conn, table: str, direct_fks: list[dict],
) -> list[dict]:
    """BFS: for each pair (table, other) without a direct high-confidence
    FK, search the global FK graph for a 2-hop path."""
    graph = _load_existing_fk_graph(conn)
    # Direct-FK set (high-confidence only) for exclusion
    direct_targets = {
        f["to_table"] for f in direct_fks if f.get("confidence") == "high"
    }
    bridges: list[dict] = []
    # For each potential 2-hop via intermediate X:
    # table → X → Y  (only X → Y edges come from graph[X])
    # We look at our own direct_fks (table → X); then consult graph[X] for X → Y.
    for mid_fk in direct_fks:
        if mid_fk.get("confidence") != "high":
            continue
        mid = mid_fk["to_table"]
        for edge in graph.get(mid, []):
            y = edge["to_table"]
            if y == table or y in direct_targets:
                continue
            path = (
                f"{table}.{mid_fk['from_columns'][0]} -> "
                f"{mid}.{mid_fk['to_columns'][0]} && "
                f"{mid}.{edge['from_col']} -> {y}.{edge['to_col']}"
            )
            bridges.append({
                "between": [table, y],
                "via": mid,
                "path": path,
                "confidence": "medium",  # 2-hop inference is weaker than direct
            })
    return bridges


# ─── Rationale + DAR write ─────────────────────────────────────────────

def _build_rationale(
    table: str, pks: list, fks: list, shapes: list, bridges: list,
) -> str:
    parts = [f"Schema discovery for {table} completed."]
    if pks:
        if len(pks[0]["columns"]) == 1:
            parts.append(
                f"Primary key: {pks[0]['columns'][0]} "
                f"(confidence {pks[0]['confidence']})."
            )
        else:
            parts.append(
                f"Composite primary key: "
                f"({', '.join(pks[0]['columns'])})."
            )
    else:
        parts.append("No primary key candidate met distinct+not-null threshold.")
    high_fks = [f for f in fks if f.get("confidence") == "high"]
    med_fks = [f for f in fks if f.get("confidence") == "medium"]
    if high_fks:
        parts.append(
            f"{len(high_fks)} high-confidence FK candidate(s) found."
        )
    if med_fks:
        parts.append(f"{len(med_fks)} medium-confidence FK candidate(s) flagged.")
    if shapes:
        hd = [s for s in shapes if s["shape"] in ("header_detail", "detail_header")]
        if hd:
            parts.append(f"{len(hd)} header-detail relationship(s) confirmed with sum-match.")
    if bridges:
        parts.append(f"{len(bridges)} bridge table pattern(s) observed.")
    return " ".join(parts)


def _build_dar_row(
    *, table: str, result_json: dict, schema_version: str,
    last_source_ingestion_at: str, status: str = "success",
    error_message: str = "",
) -> dict:
    run_id = (
        f"schema_discovery_"
        f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
        f"{table}"
    )
    return {
        "id": _next_dar_id(),
        "analysis_type": _ANALYSIS_TYPE,
        "executed_at_utc": _iso(_now_utc_naive()),
        "result_json": json.dumps(result_json, default=str),
        "promoted": "false",
        "promoted_at_utc": "",
        "promoted_to_target_id": "",
        "run_id": run_id,
        "query_sql": f"-- schema_discovery on {SOURCE_SCHEMA}.{table}",
        "row_count": "",
        "error_message": error_message,
        "status": status,
        "superseded_by": "",
        "executed_by": "run_schema_discovery_analysis.py",
        "schema_version": schema_version,
        "source_tables": table,
        "domain_name": _DOMAIN_NAME,
        "last_source_ingestion_at": last_source_ingestion_at,
    }


def _append_dar(row: dict) -> None:
    header_needed = not _DAR_CSV.exists() or _DAR_CSV.stat().st_size == 0
    existing: list[dict] = []
    if _DAR_CSV.exists():
        with _DAR_CSV.open("r", encoding="utf-8", newline="") as f:
            existing = list(csv.DictReader(f))
    existing.append(row)
    tmp = _DAR_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_DAR_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(existing)
    os.replace(tmp, _DAR_CSV)


# ─── Analyzer entry points ─────────────────────────────────────────────

def analyze_table(conn, table: str) -> int:
    """Full-mode analysis. Returns 0 on success (DAR emitted or skipped)."""
    cols = _table_columns(conn, table)
    total = _row_count(conn, table)
    if total == 0 or len(cols) < 2:
        from _skipped_dar import build_skipped_dar_row  # noqa: E402
        skipped = build_skipped_dar_row(
            dar_id=_next_dar_id(),
            analysis_type=_ANALYSIS_TYPE,
            source_tables=table,
            skip_reason=(
                f"insufficient structure for relational analysis "
                f"(rows={total}, cols={len(cols)})"
            ),
            schema_version=_schema_version(conn, table),
            last_source_ingestion_at="",
            executed_by="run_schema_discovery_analysis.py",
            domain_name=_DOMAIN_NAME,
        )
        _append_dar(skipped)
        supersede_prior_dars_for_table(
            skipped["analysis_type"], skipped["source_tables"],
            [skipped["id"]],
        )
        print(f"  [skip] {table}: {skipped['id']}")
        return 0

    all_tables = _source_tables(conn)
    pks = _discover_pk_candidates(conn, table)
    fks = _discover_fk_candidates(conn, table, all_tables)
    shapes = _classify_relationship_shapes(conn, table, fks)
    bridges = _discover_bridges(conn, table, fks)

    result_json = {
        "pk_candidates": pks,
        "fk_candidates": fks,
        "relationship_shapes": shapes,
        "bridge_tables": bridges,
        "rationale": _build_rationale(table, pks, fks, shapes, bridges),
        "blockers_addressed": [],
    }
    row = _build_dar_row(
        table=table, result_json=result_json,
        schema_version=_schema_version(conn, table),
        last_source_ingestion_at="",
    )
    _append_dar(row)
    supersede_prior_dars_for_table(
        row["analysis_type"], row["source_tables"], [row["id"]],
    )
    print(
        f"  [ok] {row['id']} schema_discovery {table}: "
        f"PK={len(pks)} FK={len(fks)} shapes={len(shapes)} bridges={len(bridges)}"
    )
    return 0


def refresh_bridges(conn, table: str) -> int:
    """bridges_only mode: reuse the prior schema_discovery DAR's PK/FK/shape
    content but recompute bridges against the current FK graph. Writes a
    fresh DAR and supersedes the prior row."""
    try:
        rows = conn.execute(
            "SELECT id, result_json FROM main_seeds.domain_analysis_results "
            "WHERE analysis_type = 'schema_discovery' "
            "  AND status = 'success' "
            "  AND LOWER(source_tables) = LOWER(?) "
            "ORDER BY executed_at_utc DESC LIMIT 1",
            [table],
        ).fetchall()
    except duckdb.Error:
        rows = []
    if not rows:
        print(f"  [skip] {table}: no prior schema_discovery DAR to refresh")
        return 0
    prior = rows[0]
    try:
        payload = json.loads(prior[1]) if prior[1] else {}
    except json.JSONDecodeError:
        print(f"  [err]  {table}: prior DAR result_json unparseable")
        return 1

    pks = payload.get("pk_candidates") or []
    fks = payload.get("fk_candidates") or []
    shapes = payload.get("relationship_shapes") or []
    new_bridges = _discover_bridges(conn, table, fks)

    result_json = {
        "pk_candidates": pks,
        "fk_candidates": fks,
        "relationship_shapes": shapes,
        "bridge_tables": new_bridges,
        "rationale": _build_rationale(table, pks, fks, shapes, new_bridges)
        + " [bridges refreshed]",
        "blockers_addressed": [],
    }
    new_row = _build_dar_row(
        table=table, result_json=result_json,
        schema_version=_schema_version(conn, table),
        last_source_ingestion_at="",
    )
    _append_dar(new_row)
    supersede_prior_dars_for_table(
        new_row["analysis_type"], new_row["source_tables"],
        [new_row["id"]],
    )
    print(
        f"  [ok] {new_row['id']} schema_discovery {table} [bridges_only]: "
        f"bridges={len(new_bridges)} (supersedes {prior[0]})"
    )
    return 0


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Schema discovery analyzer.")
    p.add_argument("--table", required=True, metavar="NAME",
                   help=f"{SOURCE_SCHEMA} table to analyze (lowercase)")
    p.add_argument("--mode", choices=("full", "bridges_only"), default="full",
                   help="full (default) runs PK/FK/shape/bridge; "
                        "bridges_only re-runs only bridge detection and "
                        "supersedes the prior DAR.")
    p.add_argument("--no-parquet-sync", action="store_true",
                   help="Skip post-write parquet export")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None, conn=None) -> int:
    args = _parse_args(argv)
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB_PATH), read_only=False)
    try:
        if args.mode == "bridges_only":
            rc = refresh_bridges(conn, args.table)
        else:
            rc = analyze_table(conn, args.table)
    finally:
        if owned:
            conn.close()
    if rc == 0 and not args.no_parquet_sync:
        # KI-118 fix: previously passed seeds_changed=[...] which is not
        # a valid kwarg — TypeError was caught by try/except and printed
        # as a benign-looking warning, leaving parquet stale. Now passes
        # the real seed_name and captures the function's own warning
        # return value (None on success). Same fix-class as KI-103/105.
        from _parquet_sync import sync_parquet_and_invalidate
        sync_warning = sync_parquet_and_invalidate(
            project_root=_PROJECT_ROOT,
            seed_name="domain_analysis_results",
            source="run_schema_discovery_analysis",
        )
        if sync_warning:
            print(
                f"WARN: domain_analysis_results parquet sync incomplete; "
                f"dashboard may be stale until next sync: {sync_warning}",
                file=sys.stderr,
            )
    return rc


if __name__ == "__main__":
    sys.exit(main())
