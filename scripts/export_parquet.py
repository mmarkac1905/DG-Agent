"""Export all DuckDB tables to Parquet files for Streamlit to read.

Writes every table from every schema in cpe_analytics.duckdb into a
single Parquet file per table under data/parquet/{schema}/{table}.parquet.
Streamlit reads these Parquet files via an in-memory DuckDB engine in
app/db.py, which means cpe_analytics.duckdb is never opened (and never
locked) by the dashboard.

Usage:
  python scripts/export_parquet.py

Run after dbt run / dbt seed / any pipeline step that mutates the
database. scripts/run_pipeline.py wraps dbt + this export.
"""
import time
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "cpe_analytics.duckdb"
PARQUET_DIR = ROOT / "data" / "parquet"

# Schemas to skip — they are not interesting for Streamlit and would
# create hundreds of tiny files.
SKIP_SCHEMAS = {"information_schema", "pg_catalog"}


def export_all():
    """Export every table in every schema to data/parquet/<schema>/<table>.parquet.

    Returns (exported, errors, by_schema_counts).
    """
    start = time.time()
    print(f"Exporting DuckDB tables to Parquet -> {PARQUET_DIR.relative_to(ROOT)}")

    PARQUET_DIR.mkdir(parents=True, exist_ok=True)

    conn = duckdb.connect(str(DB_PATH), read_only=True)
    exported_tables: list[tuple[str, str]] = []
    try:
        tables = conn.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY table_schema, table_name
            """
        ).fetchall()

        by_schema: dict = {}
        exported = 0
        errors = 0

        for schema, table in tables:
            if schema in SKIP_SCHEMAS:
                continue

            schema_dir = PARQUET_DIR / schema
            schema_dir.mkdir(parents=True, exist_ok=True)
            parquet_path = schema_dir / f"{table}.parquet"

            try:
                # Escape identifiers with double quotes so names with
                # unusual characters still load
                conn.execute(
                    f'COPY (SELECT * FROM "{schema}"."{table}") '
                    f"TO '{parquet_path.as_posix()}' "
                    "(FORMAT PARQUET, COMPRESSION ZSTD)"
                )
                exported += 1
                exported_tables.append((schema, table))
                by_schema[schema] = by_schema.get(schema, 0) + 1
            except Exception as e:
                errors += 1
                print(f"  [error] {schema}.{table}: {e}")

        # --- Prune parquet files whose DB table no longer exists.
        # COPY only ever writes; without this, a decommissioned seed's
        # parquet ghost survives forever and the app (parquet-backed
        # connection) keeps serving a table the DB dropped months ago
        # (analyst finding: 6 ghosts from a 2026-04 decommission).
        live = set(exported_tables)
        pruned = 0
        for stale in PARQUET_DIR.glob("*/*.parquet"):
            key = (stale.parent.name, stale.stem)
            if key not in live and stale.parent.name not in SKIP_SCHEMAS:
                stale.unlink()
                pruned += 1
                print(f"  [pruned] {stale.parent.name}.{stale.stem} "
                      "(no longer in the database)")
        if pruned:
            print(f"  Pruned {pruned} stale parquet file(s).")

        # --- Sidecar: row-count per exported table (design §OQ2 resolution).
        # No content_sha256 because parquet export is unconditional; sidecar
        # is forensic, not staleness-gating. Uses the still-open conn.
        import sys as _sys, hashlib as _hashlib
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _sidecar import (
            compute_duckdb_row_count, current_git_head_sha, now_iso_utc,
            write_sidecar,
        )
        _inputs_payload: dict = {}
        for _schema, _table in exported_tables:
            _inputs_payload[f"{_schema}.{_table}"] = {
                "row_count": compute_duckdb_row_count(conn, _schema, _table),
            }
        # Overall hash derived from row counts only — §OQ2 resolution.
        _canonical = "\n".join(
            f"{k}={_inputs_payload[k]['row_count']}"
            for k in sorted(_inputs_payload)
        )
        _overall = _hashlib.sha256(_canonical.encode("utf-8")).hexdigest()[:12]
    finally:
        conn.close()

    elapsed = time.time() - start
    print(f"\n  Exported: {exported} tables ({errors} errors)")
    for s in sorted(by_schema):
        print(f"    {s:14} {by_schema[s]:3} tables")
    print(f"  Location: {PARQUET_DIR.relative_to(ROOT)}")
    print(f"  Time: {elapsed:.1f}s")

    # Write sidecar after conn.close() so failures in sidecar write don't
    # strand an open DuckDB handle. Errors here raise loud per _sidecar.py
    # contract.
    write_sidecar("parquet", {
        "artifact": "parquet",
        "schema_version": 1,
        "built_at_utc": now_iso_utc(),
        "git_head_sha": current_git_head_sha(),
        "inputs": _inputs_payload,
        "overall_hash": _overall,
        "build_duration_sec": round(elapsed, 3),
        "warnings": [f"{errors} export errors"] if errors else [],
    })

    return exported, errors, by_schema


def export_table(schema: str, table: str) -> bool:
    """Export a single table to Parquet. Used by save_and_sync()."""
    schema_dir = PARQUET_DIR / schema
    schema_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = schema_dir / f"{table}.parquet"

    conn = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        conn.execute(
            f'COPY (SELECT * FROM "{schema}"."{table}") '
            f"TO '{parquet_path.as_posix()}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        return True
    except Exception as e:
        print(f"  [error] export_table({schema}.{table}): {e}")
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    export_all()
