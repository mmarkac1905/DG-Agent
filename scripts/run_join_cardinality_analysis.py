"""Direction D §5 (Phase 2a) — join_cardinality analyzer.

Emits domain_analysis_results rows with analysis_type='join_cardinality'.
Per pair of raw_sap tables, enumerate candidate join keys (direct +
2-hop bridge) from three sources unified, sample fanout empirically,
and classify into one of four buckets.

Spec: context/direction_d_spec.md §5.
Parent issue: #86. Prerequisite shipped 8cfe7e4 (#87 F10 fix).

Buckets (§5.4):
  per_record_key      avg ∈ [0.9, 1.1] AND stddev < 0.5 AND matched > 0.8
  header_detail       avg ∈ [1.5, 100] AND stddev/avg < 1.0
  catastrophic_fanout avg > 100 OR stddev > avg
  no_signal           matched/sampled < 0.1

Boundary cases classify to the more-conservative bucket (§5.4 footer).

CLI:
  python scripts/run_join_cardinality_analysis.py                    # all raw pairs
  python scripts/run_join_cardinality_analysis.py --pairs equi,mseg  # single pair
  python scripts/run_join_cardinality_analysis.py --tables a,b,c     # restrict scope

Exit codes:
  0 — success
  1 — DuckDB or argument error
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

from _source_config import SOURCE_SCHEMA

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SEED_DIR = _PROJECT_ROOT / "dbt" / "seeds"
_DB_PATH = _PROJECT_ROOT / "cpe_analytics.duckdb"
_DAR_CSV = _SEED_DIR / "domain_analysis_results.csv"

sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from _dar_supersede import supersede_prior_dars_for_table  # noqa: E402

_DAR_FIELDS: list[str] = [
    "id", "analysis_type", "executed_at_utc", "result_json",
    "promoted", "promoted_at_utc", "promoted_to_target_id",
    "run_id", "query_sql", "row_count", "error_message", "status",
    "superseded_by", "executed_by", "schema_version",
    "source_tables", "domain_name",
    "last_source_ingestion_at",
]

_ANALYSIS_TYPE = "join_cardinality"
_DOMAIN_NAME = "cardinality"

# Per §5.2: blacklist non-key shared columns (audit/system fields).
_BLACKLIST = frozenset({"MANDT", "ERNAM", "ERDAT", "UZEIT", "ERZEIT",
                        "LOEKZ"})

# Per §5.3: sampling bounds.
_SAMPLE_FLOOR = 50
_SAMPLE_CAP = 500


# ─── timestamp + id helpers ────────────────────────────────────────────

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


# ─── type families (Amendment 2 rule 2(b)) ─────────────────────────────

def _type_family(data_type: str) -> str:
    """Coarse semantic family for bridge type-compatibility check.

    Per Direction D Amendment 2 rule 2(b): bridge key pairs must match
    family. SAP fixtures rarely exercise this in practice (shared-name
    columns share types), so this is defense-in-depth.
    """
    up = (data_type or "").upper()
    if not up:
        return "other"
    if up.startswith("VARCHAR") or up.startswith("CHAR") or up.startswith("TEXT") \
       or up.startswith("STRING") or up.startswith("BPCHAR"):
        return "string"
    if up.startswith("DECIMAL") or up.startswith("NUMERIC") \
       or up in {"DOUBLE", "INTEGER", "BIGINT", "FLOAT", "REAL",
                 "HUGEINT", "SMALLINT", "TINYINT"}:
        return "numeric"
    if up.startswith("DATE") or up.startswith("TIMESTAMP") or up.startswith("TIME"):
        return "temporal"
    return "other"


# ─── schema introspection ──────────────────────────────────────────────

def _source_tables(conn) -> list[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema='{SOURCE_SCHEMA}' ORDER BY table_name"
    ).fetchall()
    return [r[0].lower() for r in rows]


def _table_columns(conn, table: str) -> dict[str, str]:
    """Returns {original_case_column_name: data_type_uppercase}."""
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        f"WHERE table_schema='{SOURCE_SCHEMA}' AND LOWER(table_name)=LOWER(?)",
        [table],
    ).fetchall()
    return {c: (t or "").upper() for c, t in rows}


def _row_count(conn, table: str) -> int:
    safe = f'{SOURCE_SCHEMA}."{table}"'
    return int(conn.execute(f"SELECT COUNT(*) FROM {safe}").fetchone()[0] or 0)


def _schema_version_pair(conn, t1: str, t2: str) -> str:
    rows = conn.execute(
        "SELECT table_name, column_name, data_type "
        "FROM information_schema.columns "
        f"WHERE table_schema='{SOURCE_SCHEMA}' "
        "AND LOWER(table_name) IN (LOWER(?), LOWER(?)) "
        "ORDER BY table_name, ordinal_position",
        [t1, t2],
    ).fetchall()
    payload = ",".join(f"{tn}.{c}:{t}" for tn, c, t in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# ─── candidate enumeration (§5.2) ──────────────────────────────────────

def _shared_name_keys(t1_cols: dict[str, str],
                      t2_cols: dict[str, str]) -> set[str]:
    """Source A: exact-name shared columns (uppercased), minus blacklist."""
    t1_up = {c.upper() for c in t1_cols}
    t2_up = {c.upper() for c in t2_cols}
    return (t1_up & t2_up) - _BLACKLIST


def _schema_discovery_fks(conn, table: str) -> list[dict]:
    """Read latest successful schema_discovery DAR for table; return its
    fk_candidates list. Returns [] if no DAR.
    """
    row = conn.execute("""
        SELECT result_json FROM main_seeds.domain_analysis_results
        WHERE analysis_type = 'schema_discovery'
          AND status = 'success'
          AND LOWER(source_tables) = LOWER(?)
        ORDER BY executed_at_utc DESC
        LIMIT 1
    """, [table]).fetchone()
    if not row:
        return []
    try:
        d = json.loads(row[0]) or {}
    except (json.JSONDecodeError, TypeError):
        return []
    return d.get("fk_candidates") or []


def _semantic_model_key_cols(conn, table: str) -> set[str]:
    """Source C: semantic_model role='key' columns for table (uppercased)."""
    try:
        rows = conn.execute("""
            SELECT column_name FROM main_seeds.source_column_roles
            WHERE LOWER(table_name) = LOWER(?)
              AND role = 'key'
        """, [table]).fetchall()
    except duckdb.Error:
        return set()
    return {r[0].upper() for r in rows} - _BLACKLIST


def _direct_candidates(conn, t1: str, t2: str) -> list[dict]:
    """Enumerate direct join candidates between t1 and t2 from all 3 sources.

    Dedup key (per §5.2): (t1, t2, key_columns, kind, bridge_via).
    Same column from multiple sources merges into one candidate; `source`
    becomes the union.
    """
    t1_cols = _table_columns(conn, t1)
    t2_cols = _table_columns(conn, t2)
    if not t1_cols or not t2_cols:
        return []

    # candidates_dict key: (tuple(t1_keys_upper), tuple(t2_keys_upper))
    candidates: dict[tuple, dict] = {}

    def _ensure(t1_keys: tuple[str, ...], t2_keys: tuple[str, ...]) -> dict:
        key = (t1_keys, t2_keys)
        if key not in candidates:
            candidates[key] = {
                "kind": "direct",
                "bridge_via": None,
                "key_columns_t1": list(t1_keys),
                "key_columns_t2": list(t2_keys),
                "source": [],
                "referential_integrity_pct": None,
            }
        return candidates[key]

    # Source A — exact-name shared
    for col_up in _shared_name_keys(t1_cols, t2_cols):
        c = _ensure((col_up,), (col_up,))
        if "shared_name" not in c["source"]:
            c["source"].append("shared_name")

    # Source B — schema_discovery FK hints (both directions)
    for fk in _schema_discovery_fks(conn, t1):
        to_table = (fk.get("to_table") or "").lower()
        if to_table != t2.lower():
            continue
        from_cols = tuple(c.upper() for c in fk.get("from_columns") or [])
        to_cols = tuple(c.upper() for c in fk.get("to_columns") or [])
        if not from_cols or not to_cols or len(from_cols) != len(to_cols):
            continue
        c = _ensure(from_cols, to_cols)
        if "schema_discovery_fk" not in c["source"]:
            c["source"].append("schema_discovery_fk")
        ri = fk.get("referential_integrity_pct")
        if ri is not None:
            prev = c["referential_integrity_pct"]
            c["referential_integrity_pct"] = (
                ri if prev is None else max(prev, float(ri))
            )

    for fk in _schema_discovery_fks(conn, t2):
        to_table = (fk.get("to_table") or "").lower()
        if to_table != t1.lower():
            continue
        from_cols = tuple(c.upper() for c in fk.get("from_columns") or [])
        to_cols = tuple(c.upper() for c in fk.get("to_columns") or [])
        if not from_cols or not to_cols or len(from_cols) != len(to_cols):
            continue
        c = _ensure(to_cols, from_cols)  # flipped: t1↔t2 perspective
        if "schema_discovery_fk" not in c["source"]:
            c["source"].append("schema_discovery_fk")
        ri = fk.get("referential_integrity_pct")
        if ri is not None:
            prev = c["referential_integrity_pct"]
            c["referential_integrity_pct"] = (
                ri if prev is None else max(prev, float(ri))
            )

    # Source C — semantic_model role='key' intersection
    t1_keys = _semantic_model_key_cols(conn, t1)
    t2_keys = _semantic_model_key_cols(conn, t2)
    for col_up in (t1_keys & t2_keys):
        c = _ensure((col_up,), (col_up,))
        if "semantic_model_role" not in c["source"]:
            c["source"].append("semantic_model_role")

    return list(candidates.values())


def _bridge_candidates(conn, t1: str, t2: str,
                       all_tables: list[str]) -> list[dict]:
    """Enumerate 2-hop bridges via every t3 in raw_sap (t3 != t1, t2).

    A bridge candidate: t3 has direct candidates with both t1 and t2.
    Cartesian product of (t1↔t3 candidates) × (t3↔t2 candidates) yields
    bridge candidates. 2-hop only per §5.2 (3-hop deferred).

    Direction D Amendment 2 prunes bridges with two filters:
      (a) Both t3 sides (k3_left, k3_right) must be role='key' in
          main_seeds.source_column_roles. t1 and t2 keys are not
          filtered — they link INTO t3 and don't need role='key'
          themselves.
      (b) Type-family compatibility across each leg (t1.k1 ↔ t3.k3_left
          and t3.k3_right ↔ t2.k2). Same family or candidate dropped.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    t1_cols = _table_columns(conn, t1)
    t2_cols = _table_columns(conn, t2)

    for t3 in all_tables:
        if t3 == t1 or t3 == t2:
            continue
        c13 = _direct_candidates(conn, t1, t3)
        if not c13:
            continue
        c23 = _direct_candidates(conn, t3, t2)
        if not c23:
            continue
        # Rule 2(a): pre-load t3's role='key' set + columns once.
        t3_role_keys = _semantic_model_key_cols(conn, t3)
        if not t3_role_keys:
            continue  # no key columns on t3 → no bridges via this t3
        t3_cols = _table_columns(conn, t3)
        for cand13 in c13:
            for cand23 in c23:
                t1_keys = tuple(cand13["key_columns_t1"])
                t3_keys_left = tuple(cand13["key_columns_t2"])
                t3_keys_right = tuple(cand23["key_columns_t1"])
                t2_keys = tuple(cand23["key_columns_t2"])
                # Rule 2(a): both t3 sides must have role='key'.
                if not all(k.upper() in t3_role_keys for k in t3_keys_left):
                    continue
                if not all(k.upper() in t3_role_keys for k in t3_keys_right):
                    continue
                # Rule 2(b): type families must match across each leg.
                if not _bridge_types_compatible(
                    t1_cols, t1_keys, t3_cols, t3_keys_left,
                    t3_keys_right, t2_cols, t2_keys,
                ):
                    continue
                key = (t1_keys, t2_keys, t3, t3_keys_left, t3_keys_right)
                if key in seen:
                    continue
                seen.add(key)
                merged_source = sorted(set(cand13["source"])
                                       | set(cand23["source"]))
                out.append({
                    "kind": "bridge",
                    "bridge_via": t3,
                    "key_columns_t1": list(t1_keys),
                    "key_columns_t2": list(t2_keys),
                    "bridge_keys_left": list(t3_keys_left),
                    "bridge_keys_right": list(t3_keys_right),
                    "source": merged_source,
                    "referential_integrity_pct": None,
                })
    return out


def _bridge_types_compatible(t1_cols: dict[str, str],
                              k1: tuple[str, ...],
                              t3_cols: dict[str, str],
                              k3_left: tuple[str, ...],
                              k3_right: tuple[str, ...],
                              t2_cols: dict[str, str],
                              k2: tuple[str, ...]) -> bool:
    """Rule 2(b) defense-in-depth — bridge legs must match type family."""
    def _type_for(cols: dict[str, str], col_up: str) -> str:
        for c, t in cols.items():
            if c.upper() == col_up.upper():
                return _type_family(t)
        return "other"

    if len(k1) != len(k3_left) or len(k3_right) != len(k2):
        return False
    for a, b in zip(k1, k3_left):
        if _type_for(t1_cols, a) != _type_for(t3_cols, b):
            return False
    for a, b in zip(k3_right, k2):
        if _type_for(t3_cols, a) != _type_for(t2_cols, b):
            return False
    return True


# ─── sampling + fanout measurement (§5.3) ─────────────────────────────

def _resolve_case_keys(t_cols: dict[str, str],
                       upper_keys: list[str]) -> Optional[list[str]]:
    """Map upper-cased key names back to original column case so SQL
    quoting works regardless of case folding. Returns None if any key
    is missing in the table (defensive against schema drift).
    """
    up_to_orig = {c.upper(): c for c in t_cols}
    out: list[str] = []
    for k in upper_keys:
        if k not in up_to_orig:
            return None
        out.append(up_to_orig[k])
    return out


def _measure_direct(conn, t_small: str, k_small: list[str],
                    t_large: str, k_large: list[str]) -> dict:
    """Sample distinct keys from t_small, measure fanout on t_large.

    Per §5.3: 50–500 distinct keys, floor at 50 (or all if <50).
    Returns measurement dict consumed by _classify and the DAR builder.
    """
    safe_small = f'{SOURCE_SCHEMA}."{t_small}"'
    safe_large = f'{SOURCE_SCHEMA}."{t_large}"'
    small_keys_sql = ", ".join(f's."{c}"' for c in k_small)
    large_keys_sql = ", ".join(f'l."{c}"' for c in k_large)
    join_on = " AND ".join(
        f's."{a}" = l."{b}"'
        for a, b in zip(k_small, k_large)
    )

    # Use a deterministic-enough sample: ORDER BY a hash of the keys
    # (cheaper than RANDOM() over large tables and reproducible per
    # schema, which helps test stability).
    hash_expr = " || '|' || ".join(
        f'COALESCE(CAST("{c}" AS VARCHAR), \'\')' for c in k_small
    )

    distinct_n = int(conn.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT {', '.join(chr(34)+c+chr(34) for c in k_small)} "
        f"FROM {safe_small} WHERE {' AND '.join(chr(34)+c+chr(34)+' IS NOT NULL' for c in k_small)})"
    ).fetchone()[0] or 0)

    if distinct_n == 0:
        return {
            "sampled_keys": 0, "matched_keys": 0,
            "matched_keys_ratio": 0.0,
            "avg_fanout": 0.0, "max_fanout": 0,
            "stddev_fanout": 0.0, "sample_saturated": False,
            "distinct_keys_smaller": 0,
        }

    if distinct_n < _SAMPLE_FLOOR:
        sample_size = distinct_n
        sample_saturated = True
    else:
        sample_size = min(distinct_n, _SAMPLE_CAP)
        sample_saturated = False

    sql = f"""
        WITH samp AS (
            SELECT DISTINCT {', '.join(chr(34)+c+chr(34) for c in k_small)}
            FROM {safe_small}
            WHERE {' AND '.join(chr(34)+c+chr(34)+' IS NOT NULL' for c in k_small)}
            ORDER BY HASH({hash_expr})
            LIMIT {sample_size}
        ),
        joined AS (
            SELECT {small_keys_sql},
                   COUNT({large_keys_sql.split(',')[0].strip()}) AS fanout
            FROM samp s LEFT JOIN {safe_large} l ON {join_on}
            GROUP BY {', '.join(f's."{c}"' for c in k_small)}
        )
        SELECT COUNT(*) AS sampled,
               SUM(CASE WHEN fanout > 0 THEN 1 ELSE 0 END) AS matched,
               COALESCE(AVG(fanout::DOUBLE), 0) AS avg_fanout,
               COALESCE(MAX(fanout), 0) AS max_fanout,
               COALESCE(STDDEV_POP(fanout::DOUBLE), 0) AS stddev_fanout
        FROM joined
    """
    sampled, matched, avg_f, max_f, stddev_f = conn.execute(sql).fetchone()
    sampled_n = int(sampled or 0)
    matched_n = int(matched or 0)
    return {
        "sampled_keys": sampled_n,
        "matched_keys": matched_n,
        "matched_keys_ratio": (matched_n / sampled_n) if sampled_n else 0.0,
        "avg_fanout": float(avg_f or 0.0),
        "max_fanout": int(max_f or 0),
        "stddev_fanout": float(stddev_f or 0.0),
        "sample_saturated": sample_saturated,
        "distinct_keys_smaller": distinct_n,
    }


def _measure_bridge(conn, t1: str, k1: list[str],
                    t3: str, k3_left: list[str], k3_right: list[str],
                    t2: str, k2: list[str]) -> dict:
    """Sample distinct keys from t1, join through bridge t3 to t2,
    measure total t2 rows reached per t1 key.

    t1.k1 ↔ t3.k3_left, then t3.k3_right ↔ t2.k2.
    """
    safe_t1 = f'{SOURCE_SCHEMA}."{t1}"'
    safe_t2 = f'{SOURCE_SCHEMA}."{t2}"'
    safe_t3 = f'{SOURCE_SCHEMA}."{t3}"'
    join_t1_t3 = " AND ".join(
        f's."{a}" = three."{b}"' for a, b in zip(k1, k3_left)
    )
    join_t3_t2 = " AND ".join(
        f'three."{a}" = two."{b}"' for a, b in zip(k3_right, k2)
    )
    hash_expr = " || '|' || ".join(
        f'COALESCE(CAST("{c}" AS VARCHAR), \'\')' for c in k1
    )

    distinct_n = int(conn.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT {', '.join(chr(34)+c+chr(34) for c in k1)} "
        f"FROM {safe_t1} WHERE {' AND '.join(chr(34)+c+chr(34)+' IS NOT NULL' for c in k1)})"
    ).fetchone()[0] or 0)

    if distinct_n == 0:
        return {
            "sampled_keys": 0, "matched_keys": 0,
            "matched_keys_ratio": 0.0,
            "avg_fanout": 0.0, "max_fanout": 0,
            "stddev_fanout": 0.0, "sample_saturated": False,
            "distinct_keys_smaller": 0,
        }

    if distinct_n < _SAMPLE_FLOOR:
        sample_size = distinct_n
        sample_saturated = True
    else:
        sample_size = min(distinct_n, _SAMPLE_CAP)
        sample_saturated = False

    sql = f"""
        WITH samp AS (
            SELECT DISTINCT {', '.join(chr(34)+c+chr(34) for c in k1)}
            FROM {safe_t1}
            WHERE {' AND '.join(chr(34)+c+chr(34)+' IS NOT NULL' for c in k1)}
            ORDER BY HASH({hash_expr})
            LIMIT {sample_size}
        ),
        joined AS (
            SELECT {', '.join(f's."{c}"' for c in k1)},
                   COUNT(two."{k2[0]}") AS fanout
            FROM samp s
            LEFT JOIN {safe_t3} three ON {join_t1_t3}
            LEFT JOIN {safe_t2} two   ON {join_t3_t2}
            GROUP BY {', '.join(f's."{c}"' for c in k1)}
        )
        SELECT COUNT(*) AS sampled,
               SUM(CASE WHEN fanout > 0 THEN 1 ELSE 0 END) AS matched,
               COALESCE(AVG(fanout::DOUBLE), 0) AS avg_fanout,
               COALESCE(MAX(fanout), 0) AS max_fanout,
               COALESCE(STDDEV_POP(fanout::DOUBLE), 0) AS stddev_fanout
        FROM joined
    """
    sampled, matched, avg_f, max_f, stddev_f = conn.execute(sql).fetchone()
    sampled_n = int(sampled or 0)
    matched_n = int(matched or 0)
    return {
        "sampled_keys": sampled_n,
        "matched_keys": matched_n,
        "matched_keys_ratio": (matched_n / sampled_n) if sampled_n else 0.0,
        "avg_fanout": float(avg_f or 0.0),
        "max_fanout": int(max_f or 0),
        "stddev_fanout": float(stddev_f or 0.0),
        "sample_saturated": sample_saturated,
        "distinct_keys_smaller": distinct_n,
    }


# ─── classification (§5.4) ─────────────────────────────────────────────

def _classify(m: dict) -> str:
    """Per §5.4 with explicit boundary handling.

    Order matters: no_signal first (most conservative for low-evidence),
    then catastrophic_fanout (most conservative for high-fanout),
    then header_detail, then per_record_key. Anything that doesn't fit
    a labeled bucket falls back to catastrophic_fanout if avg > 1.1
    (slight-fanout uncertainty → conservative); else no_signal.
    """
    avg = m["avg_fanout"]
    stddev = m["stddev_fanout"]
    matched_ratio = m["matched_keys_ratio"]

    if matched_ratio < 0.1:
        return "no_signal"
    if avg > 100 or stddev > avg:
        return "catastrophic_fanout"
    if 1.5 <= avg <= 100 and avg > 0 and (stddev / avg) < 1.0:
        return "header_detail"
    if 0.9 <= avg <= 1.1 and stddev < 0.5 and matched_ratio > 0.8:
        return "per_record_key"
    # Boundary fallback (§5.4 footer): more-conservative bucket.
    if avg > 1.1:
        return "header_detail"
    return "no_signal"


def _rationale(fanout_class: str, candidate: dict, m: dict,
               t1: str, t2: str) -> str:
    keys = "+".join(candidate["key_columns_t1"])
    if candidate["kind"] == "bridge":
        bridge = candidate["bridge_via"]
        if fanout_class == "catastrophic_fanout":
            return (f"Bridge {t1}->{bridge}->{t2} on {keys} fans out "
                    f"avg {m['avg_fanout']:.1f}x (max {m['max_fanout']}). "
                    f"Cartesian risk; not a viable bridge.")
        if fanout_class == "no_signal":
            return (f"Bridge {t1}->{bridge}->{t2} on {keys} matched "
                    f"{m['matched_keys']}/{m['sampled_keys']} sampled keys; "
                    f"insufficient signal — bridge is structural only.")
        if fanout_class == "per_record_key":
            return (f"Bridge {t1}->{bridge}->{t2} on {keys} resolves to "
                    f"avg {m['avg_fanout']:.2f} t2 rows per t1 key; safe.")
        return (f"Bridge {t1}->{bridge}->{t2} on {keys}: "
                f"avg {m['avg_fanout']:.1f}x, stddev {m['stddev_fanout']:.1f}.")
    # direct
    if fanout_class == "catastrophic_fanout":
        return (f"{keys} shared across {m['distinct_keys_smaller']} distinct "
                f"values; each expands to ~{m['avg_fanout']:.0f} rows in {t2}. "
                f"Classification code, not per-record key.")
    if fanout_class == "no_signal":
        return (f"{keys} matched only {m['matched_keys']}/{m['sampled_keys']} "
                f"sampled keys between {t1} and {t2}; integrity is structural, "
                f"data is empty/aspirational.")
    if fanout_class == "per_record_key":
        return (f"{keys} avg {m['avg_fanout']:.2f}x, stddev "
                f"{m['stddev_fanout']:.2f}; safe per-record join.")
    if fanout_class == "header_detail":
        return (f"{keys} avg {m['avg_fanout']:.1f}x with bounded variance "
                f"(stddev/avg {m['stddev_fanout']/max(m['avg_fanout'],1e-9):.2f}); "
                f"1:N relationship — safe with aggregation.")
    return (f"{keys}: avg {m['avg_fanout']:.1f}, stddev {m['stddev_fanout']:.1f}, "
            f"matched {m['matched_keys']}/{m['sampled_keys']}.")


# ─── DAR emission ─────────────────────────────────────────────────────

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


def _build_dar(t1: str, t2: str, candidate: dict, m: dict,
               schema_version: str, run_id: str,
               row_counts: dict[str, int],
               extra: Optional[dict] = None) -> dict:
    fanout_class = _classify(m)
    finding = {
        "t1": t1,
        "t2": t2,
        "kind": candidate["kind"],
        "bridge_via": candidate.get("bridge_via"),
        "key_columns_t1": candidate["key_columns_t1"],
        "key_columns_t2": candidate["key_columns_t2"],
        "source": candidate.get("source") or [],
        "referential_integrity_pct": candidate.get("referential_integrity_pct"),
        "sample_size": m["sampled_keys"],
        "sample_saturated": m["sample_saturated"],
        "matched_keys": m["matched_keys"],
        "matched_keys_ratio": round(m["matched_keys_ratio"], 4),
        "avg_fanout": round(m["avg_fanout"], 2),
        "max_fanout": m["max_fanout"],
        "stddev_fanout": round(m["stddev_fanout"], 2),
        "fanout_class": fanout_class,
        "source_row_counts": row_counts,
        "rationale": _rationale(fanout_class, candidate, m, t1, t2),
        "schema_version": schema_version,
        "blockers_addressed": [],
    }
    if candidate["kind"] == "bridge":
        finding["bridge_keys_left"] = candidate.get("bridge_keys_left", [])
        finding["bridge_keys_right"] = candidate.get("bridge_keys_right", [])
    if extra:
        finding.update(extra)
    return {
        "id": _next_dar_id(),
        "analysis_type": _ANALYSIS_TYPE,
        "executed_at_utc": _iso(_now_utc_naive()),
        "result_json": json.dumps(finding, separators=(",", ":")),
        "promoted": "false",
        "promoted_at_utc": "",
        "promoted_to_target_id": "",
        "run_id": run_id,
        "query_sql": (
            f"-- join_cardinality {candidate['kind']} on {t1}/{t2} via "
            f"{'+'.join(candidate['key_columns_t1'])}"
            + (f" through {candidate.get('bridge_via')}"
               if candidate["kind"] == "bridge" else "")
        ),
        "row_count": str(m["sampled_keys"]),
        "error_message": "",
        "status": "success",
        "superseded_by": "",
        "executed_by": "run_join_cardinality_analysis.py",
        "schema_version": schema_version,
        "source_tables": ",".join(sorted([t1.lower(), t2.lower()])),
        "domain_name": _DOMAIN_NAME,
        "last_source_ingestion_at": "",
    }


# ─── per-pair driver ──────────────────────────────────────────────────

def analyze_pair(conn, t1: str, t2: str,
                 all_tables: Optional[list[str]] = None) -> int:
    """Analyze a single pair via Direction D Amendment 2 two-pass:

      Pass 1: enumerate + measure + emit all DIRECT candidates.
      Short-circuit: if any direct classified as per_record_key,
                     skip Pass 2 entirely.
      Pass 2: enumerate + measure + emit BRIDGE candidates with rule
              2(a) role filter and rule 2(b) type-family check applied
              inside _bridge_candidates.

    Returns count of DARs emitted. Caller orders t1 < t2 lex.
    """
    t1, t2 = t1.lower(), t2.lower()
    if t1 >= t2:
        t1, t2 = t2, t1

    if all_tables is None:
        all_tables = _source_tables(conn)

    direct = _direct_candidates(conn, t1, t2)

    t1_cols = _table_columns(conn, t1)
    t2_cols = _table_columns(conn, t2)
    n1 = _row_count(conn, t1)
    n2 = _row_count(conn, t2)
    row_counts = {t1: n1, t2: n2}
    schema_version = _schema_version_pair(conn, t1, t2)
    run_id = (f"join_cardinality_"
              f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    new_ids: list[str] = []
    pair_source_tables = ",".join(sorted([t1, t2]))

    def _measure_and_emit(cand: dict) -> Optional[str]:
        """Measure the candidate, build + append the DAR, return its
        fanout_class (or None on failure)."""
        extra: Optional[dict] = None
        if cand["kind"] == "direct":
            k1_orig = _resolve_case_keys(t1_cols, cand["key_columns_t1"])
            k2_orig = _resolve_case_keys(t2_cols, cand["key_columns_t2"])
            if k1_orig is None or k2_orig is None:
                return None
            # Fanout is direction-specific: joining FROM the detail side
            # is a safe N:1 lookup even when the reverse direction
            # explodes. Measure both directions; keep the legacy
            # smaller->larger measurement as the top-level (conservative)
            # numbers, and record per-direction classes so consumers can
            # accept joins written in the safe direction.
            m12 = _measure_direct(conn, t1, k1_orig, t2, k2_orig)
            m21 = _measure_direct(conn, t2, k2_orig, t1, k1_orig)
            m = m12 if n1 <= n2 else m21
            c12, c21 = _classify(m12), _classify(m21)
            safe_direction = None
            for wanted in ("per_record_key", "header_detail"):
                for direction, cls in ((f"{t1}->{t2}", c12),
                                       (f"{t2}->{t1}", c21)):
                    if cls == wanted:
                        safe_direction = direction
                        break
                if safe_direction:
                    break
            extra = {
                "direction_fanout": {
                    f"{t1}->{t2}": {"avg_fanout": round(m12["avg_fanout"], 2),
                                    "max_fanout": m12["max_fanout"],
                                    "fanout_class": c12},
                    f"{t2}->{t1}": {"avg_fanout": round(m21["avg_fanout"], 2),
                                    "max_fanout": m21["max_fanout"],
                                    "fanout_class": c21},
                },
                "safe_direction": safe_direction,
            }
        else:  # bridge
            t3 = cand["bridge_via"]
            t3_cols = _table_columns(conn, t3)
            k1_orig = _resolve_case_keys(t1_cols, cand["key_columns_t1"])
            k3_left_orig = _resolve_case_keys(t3_cols,
                                              cand["bridge_keys_left"])
            k3_right_orig = _resolve_case_keys(t3_cols,
                                               cand["bridge_keys_right"])
            k2_orig = _resolve_case_keys(t2_cols, cand["key_columns_t2"])
            if any(x is None for x in (k1_orig, k3_left_orig,
                                       k3_right_orig, k2_orig)):
                return None
            m = _measure_bridge(conn, t1, k1_orig, t3, k3_left_orig,
                                k3_right_orig, t2, k2_orig)
        try:
            dar = _build_dar(t1, t2, cand, m, schema_version, run_id,
                             row_counts, extra=extra)
        except Exception as e:  # noqa: BLE001
            print(f"  [err]  {t1}<->{t2} candidate {cand}: {e}")
            return None
        _append_dar(dar)
        new_ids.append(dar["id"])
        kind_tag = (f"bridge via {cand['bridge_via']}"
                    if cand["kind"] == "bridge" else "direct")
        keys = "+".join(cand["key_columns_t1"])
        cls = _classify(m)
        print(f"  [ok] {dar['id']} {t1}<->{t2} {kind_tag} {keys} "
              f"-> {cls} (avg={m['avg_fanout']:.2f}, "
              f"matched={m['matched_keys']}/{m['sampled_keys']})")
        return cls

    # ── Pass 1: directs ──
    has_per_record_direct = False
    for cand in direct:
        cls = _measure_and_emit(cand)
        if cls == "per_record_key":
            has_per_record_direct = True

    # ── Pass 2: bridges (skipped per Rule 2(c) when direct per_record_key found) ──
    if not has_per_record_direct:
        bridges = _bridge_candidates(conn, t1, t2, all_tables)
        for cand in bridges:
            _measure_and_emit(cand)
    elif direct:
        print(f"  [skip-bridges] {t1}<->{t2}: direct per_record_key found, "
              f"bridge enumeration short-circuited (Rule 2(c))")

    if not new_ids:
        print(f"  [skip] {t1}<->{t2}: no candidates from any source")
        return 0

    # Supersede prior DARs for this pair (re-run safety per §5.7.8).
    flipped = supersede_prior_dars_for_table(
        _ANALYSIS_TYPE, pair_source_tables, new_ids,
    )
    if flipped:
        print(f"  [sup] superseded {flipped} prior DAR(s) for "
              f"{pair_source_tables}")
    return len(new_ids)


# ─── CLI ───────────────────────────────────────────────────────────────

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="join_cardinality analyzer.")
    p.add_argument("--tables", type=str, default=None,
                   help=f"Comma-separated subset of {SOURCE_SCHEMA} tables to "
                        f"analyze. Default: all {SOURCE_SCHEMA} tables.")
    p.add_argument("--pairs", type=str, default=None,
                   help="Single pair (e.g. equi,mseg). Mutually exclusive "
                        "with --tables.")
    p.add_argument("--no-parquet-sync", action="store_true",
                   help="Skip post-write parquet export + view "
                        "invalidation (for batch callers).")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None,
         conn: Optional[duckdb.DuckDBPyConnection] = None) -> int:
    args = _parse_args(argv)
    owned = conn is None
    if owned:
        conn = duckdb.connect(str(_DB_PATH), read_only=True)
    total = 0
    try:
        all_tables = _source_tables(conn)
        if args.pairs:
            parts = [t.strip().lower() for t in args.pairs.split(",")]
            if len(parts) != 2:
                print("ERROR: --pairs expects exactly 2 comma-separated "
                      "table names")
                return 1
            a, b = sorted(parts)
            total += analyze_pair(conn, a, b, all_tables=all_tables)
        else:
            if args.tables:
                tables = [t.strip().lower()
                          for t in args.tables.split(",") if t.strip()]
            else:
                tables = all_tables
            tables = sorted(set(tables))
            n_pairs = len(tables) * (len(tables) - 1) // 2
            print(f"Analyzing {n_pairs} pairs across {len(tables)} tables...")
            for i, t1 in enumerate(tables):
                for t2 in tables[i + 1:]:
                    total += analyze_pair(conn, t1, t2,
                                          all_tables=all_tables)
    finally:
        if owned:
            conn.close()
    print(f"\nTotal join_cardinality DARs emitted: {total}")
    if total > 0:
        from _parquet_sync import sync_parquet_and_invalidate
        sync_parquet_and_invalidate(
            project_root=_PROJECT_ROOT,
            seed_name="domain_analysis_results",
            skip=args.no_parquet_sync,
            source="run_join_cardinality_analysis",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
