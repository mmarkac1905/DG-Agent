"""In-process CSV → DuckDB → parquet helper (known_issue #81 fix).

Replaces the prior `dbt seed` subprocess path which hit Windows
exclusive-file-lock contention whenever the parent Python process
held a read-write conn to cpe_analytics.duckdb at the moment the
subprocess launched. The lock failure was caught and logged as a
warning to stderr, then silently swallowed — CSV write succeeded
but DuckDB + parquet stayed stale.

Architecture (Option A):
  - Caller-owned conn mode: pass `conn=<open RW conn>` and the helper
    uses it to CREATE OR REPLACE TABLE + INSERT FROM read_csv.
    Caller owns the conn lifecycle; we do NOT close.
  - Helper-owned conn mode: `conn=None` → helper opens a short-lived
    writer conn, does the work, closes before returning.
  - No subprocess. No lock contention. No silent failure.

Seed type handling:
  - Typed seeds (entry under dbt/seeds/schema.yml with
    config.column_types): CREATE OR REPLACE TABLE with explicit
    DDL; INSERT FROM read_csv with columns={col: type, ...} for
    strict type enforcement. Guards against the known_issue #73
    all-empty-column-sniffed-as-INTEGER class of drift.
  - Untyped seeds: CREATE OR REPLACE TABLE ... AS SELECT * FROM
    read_csv_auto('<path>', sample_size=-1). Full-file scan
    inference — strictly at least as good as dbt seed's default
    partial sniff. known_issue #82 tracks the follow-up to add
    explicit types for the remaining untyped seeds.

Call sites are unchanged (signature preserved). Callers keep
invoking `sync_parquet_and_invalidate(seed_name='...', source=...)`
as before; internals no longer subprocess.
"""
from __future__ import annotations

import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

import duckdb


@lru_cache(maxsize=1)
def _load_schema_column_types() -> dict:
    """Parse dbt/seeds/schema.yml → {seed_name: {col: type}}. Cached
    at module scope (schema.yml doesn't change mid-run)."""
    import yaml
    project_root = Path(__file__).resolve().parent.parent
    schema_path = project_root / "dbt" / "seeds" / "schema.yml"
    if not schema_path.exists():
        return {}
    with schema_path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    result: dict = {}
    for seed in (doc or {}).get("seeds", []) or []:
        name = seed.get("name")
        ct = (seed.get("config") or {}).get("column_types") or {}
        if name and ct:
            result[name] = dict(ct)
    return result


def sync_parquet_and_invalidate(
    *,
    project_root: Path,
    seed_name: Optional[str] = None,
    skip: bool = False,
    source: str = "",
    conn: Optional[duckdb.DuckDBPyConnection] = None,
) -> Optional[str]:
    """Write CSV → DuckDB → parquet in-process. Returns None on success,
    warning string on partial/total failure (matches pre-#81 contract
    so call sites don't need changes).

    Args:
      project_root: project root directory.
      seed_name: dbt seed name (matches <name>.csv in dbt/seeds/).
        None → skip DDL; fall back to subprocess bulk parquet regen
        (legacy fragile-sync path; caller-by-caller migration in
        progress — see KI-106, KI-107 for tracked follow-ups).
      skip: True → full no-op (test harness support).
      source: caller identifier for warning messages.
      conn: optional open DuckDB conn. If passed, caller owns
        lifecycle; we use it in-place. If None, helper opens + closes
        a short-lived writer conn itself.
    """
    if skip:
        return None

    warning: Optional[str] = None

    owned = conn is None
    if owned:
        db_path = project_root / "cpe_analytics.duckdb"
        conn = duckdb.connect(str(db_path), read_only=False)

    try:
        if seed_name:
            try:
                _write_seed_to_duckdb(conn, project_root, seed_name)
            except Exception as e:  # noqa: BLE001
                warning = (
                    f"[{source}] seed write failed for {seed_name}: "
                    f"{type(e).__name__}: {e}"
                )
                print(f"[WARN] {warning}", file=sys.stderr)
                return warning

            try:
                _export_seed_parquet(conn, project_root, seed_name)
            except Exception as e:  # noqa: BLE001
                warning = (
                    f"[{source}] parquet export failed for {seed_name}: "
                    f"{type(e).__name__}: {e}"
                )
                print(f"[WARN] {warning}", file=sys.stderr)
                # Don't early-return; still invalidate cache below.
        else:
            # Legacy fragile-sync path: no seed_name → bulk parquet regen.
            # Uses subprocess since export_parquet.py manages its own
            # conn lifecycle. Only safe when caller has no open RW conn
            # — same limitation as before #81 for this branch. Failures
            # log to stderr only; callers that ignore the return value
            # silently miss parquet refresh (KI-103, KI-105 class).
            # Active callers being migrated; see KI-106, KI-107.
            try:
                r = subprocess.run(
                    "python scripts/export_parquet.py",
                    cwd=str(project_root), capture_output=True, text=True,
                    timeout=180, shell=True,
                )
                if r.returncode != 0:
                    warning = (
                        f"[{source}] bulk parquet export rc={r.returncode}; "
                        f"stderr tail: {(r.stderr or '')[-300:]}"
                    )
                    print(f"[WARN] {warning}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                warning = (
                    f"[{source}] bulk parquet export failed: "
                    f"{type(e).__name__}: {e}"
                )
                print(f"[WARN] {warning}", file=sys.stderr)
    finally:
        if owned:
            conn.close()

    # Best-effort Streamlit cache invalidation. Lazy-imports so CLI
    # paths don't pull in streamlit at module load; no-op when
    # invoked outside Streamlit runtime. Streamlit's mtime-based
    # _refresh_views in db.py handles CLI-side updates independently,
    # so this call is a nice-to-have, not a correctness requirement.
    try:
        sys.path.insert(0, str(project_root / "app"))
        from db import close_connection  # type: ignore  # noqa: E402
        close_connection()
    except Exception:
        pass

    return warning


# ─── internal writers ─────────────────────────────────────────────────

def _write_seed_to_duckdb(conn, project_root: Path, seed_name: str) -> None:
    """CREATE OR REPLACE TABLE main_seeds.<seed_name> from CSV.

    Typed seeds (config.column_types in schema.yml) get explicit DDL +
    typed read_csv columns={col: type}. Untyped seeds get read_csv_auto
    with full-file inference (sample_size=-1)."""
    csv_path = project_root / "dbt" / "seeds" / f"{seed_name}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"seed CSV not found: {csv_path}")
    csv_uri = csv_path.as_posix()

    conn.execute("CREATE SCHEMA IF NOT EXISTS main_seeds")
    column_types = _load_schema_column_types().get(seed_name)

    # Quote the identifier to handle any seed name that might clash
    # with reserved words. Seed names are lowercase alphanumeric +
    # underscores in practice, but be conservative.
    qualified = f'main_seeds."{seed_name}"'

    if column_types:
        cols_ddl = ", ".join(
            [f'"{col}" {typ}' for col, typ in column_types.items()]
        )
        conn.execute(f'CREATE OR REPLACE TABLE {qualified} ({cols_ddl})')
        # DuckDB's read_csv columns= dict literal. Escape: safe because
        # column_types values come from checked-in schema.yml (trusted).
        col_dict = ", ".join(
            [f"'{col}': '{typ}'" for col, typ in column_types.items()]
        )
        conn.execute(
            f"INSERT INTO {qualified} "
            f"SELECT * FROM read_csv("
            f"'{csv_uri}', header=True, delim=',', "
            f"columns={{{col_dict}}}, nullstr='', quote='\"', escape='\"')"
        )
    else:
        # Untyped fallback: full-file scan inference.
        conn.execute(
            f"CREATE OR REPLACE TABLE {qualified} AS "
            f"SELECT * FROM read_csv_auto("
            f"'{csv_uri}', header=True, sample_size=-1)"
        )


def _export_seed_parquet(conn, project_root: Path, seed_name: str) -> None:
    """COPY main_seeds.<seed_name> to its parquet destination."""
    parquet_dir = project_root / "data" / "parquet" / "main_seeds"
    parquet_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = parquet_dir / f"{seed_name}.parquet"
    conn.execute(
        f'COPY (SELECT * FROM main_seeds."{seed_name}") '
        f"TO '{parquet_path.as_posix()}' "
        f"(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
