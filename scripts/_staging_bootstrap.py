"""Deterministic staging bootstrap for scope tables (owner design intent).

When a term's confirmed scope names source tables that have no staging
model yet, those tables get STAGED before Stage C runs — staging is the
uniform query contract, and a greenfield staging model is mechanical
(1:1 passthrough view of the source, types preserved as loaded), so it
is generated deterministically here with no LLM and no human gate.

Models are written to dbt/models/<source>/<staging>/ for a non-default
source (behind its enable flag) or dbt/models/staging/ for the SAP
demo, matching the deploy handler's isolation rule, then built with
`dbt run --select <models>`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import duckdb

from _source_config import SOURCE_SCHEMA

_ROOT = Path(__file__).resolve().parent.parent
_DBT_DIR = _ROOT / "dbt"


def _staging_prefix() -> str:
    src = SOURCE_SCHEMA
    if src.startswith("raw_"):
        src = src[len("raw_"):]
    return f"stg_{src}__"


def _staging_dir() -> Path:
    if SOURCE_SCHEMA == "raw_sap":
        return _DBT_DIR / "models" / "staging"
    src = SOURCE_SCHEMA[len("raw_"):] if SOURCE_SCHEMA.startswith("raw_") else SOURCE_SCHEMA
    return _DBT_DIR / "models" / src / "staging"


def missing_staging_tables(conn, scope_tables: list[str]) -> list[str]:
    """Scope tables with no staging model in main_staging."""
    prefix = _staging_prefix()
    missing = []
    for t in scope_tables:
        staged = f"{prefix}{t.lower()}"
        row = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='main_staging' AND LOWER(table_name)=?",
            [staged],
        ).fetchone()
        if not row:
            missing.append(t.lower())
    return missing


def _generate_model_sql(conn, table: str) -> str:
    cols = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_schema='{SOURCE_SCHEMA}' AND LOWER(table_name)=LOWER(?) "
        "ORDER BY ordinal_position",
        [table],
    ).fetchall()
    if not cols:
        raise ValueError(f"{SOURCE_SCHEMA}.{table} has no columns (missing?)")
    col_lines = ",\n".join(f'    "{c[0]}"' for c in cols)
    return (
        "{{ config(materialized='view') }}\n\n"
        "-- Auto-staged 1:1 passthrough: generated deterministically because\n"
        "-- this table entered a term's confirmed scope before any richer\n"
        "-- staging model existed (see scripts/_staging_bootstrap.py).\n"
        "SELECT\n"
        f"{col_lines}\n"
        f"FROM {{{{ source('{SOURCE_SCHEMA}', '{table}') }}}}\n"
    )


def ensure_staging_coverage(conn, scope_tables: list[str],
                            run_dbt: bool = True) -> list[str]:
    """Generate + build staging models for scope tables lacking one.

    Returns the list of staged model names created ([] when coverage was
    already complete). Never raises on dbt failure — the caller's own
    coverage checks will surface an unbuilt model.
    """
    missing = missing_staging_tables(conn, scope_tables)
    if not missing:
        return []
    prefix = _staging_prefix()
    target_dir = _staging_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for t in missing:
        name = f"{prefix}{t}"
        path = target_dir / f"{name}.sql"
        if not path.exists():
            path.write_text(_generate_model_sql(conn, t), encoding="utf-8")
        created.append(name)
        print(f"  [staging-bootstrap] generated {path.relative_to(_ROOT)}")
    if run_dbt and created:
        build_staged_models(created)
    return created


def build_staged_models(model_names: list[str]) -> bool:
    """dbt-run the given models. CALLER must not hold a DuckDB connection
    to the project DB (dbt needs the exclusive file lock). Returns True
    when the build succeeded."""
    if not model_names:
        return True
    dbt_exe = Path(sys.executable).parent / (
        "dbt.exe" if os.name == "nt" else "dbt")
    cmd = [str(dbt_exe), "run", "--select", *model_names]
    try:
        proc = subprocess.run(
            cmd, cwd=str(_DBT_DIR), env=dict(os.environ),
            capture_output=True, text=True, timeout=300,
        )
        ok = proc.returncode == 0
        tag = "ok" if ok else f"rc={proc.returncode}"
        print(f"  [staging-bootstrap] dbt run {' '.join(model_names)}: {tag}")
        if not ok:
            print(proc.stdout[-600:])
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"  [staging-bootstrap] dbt run failed: {e}")
        return False
