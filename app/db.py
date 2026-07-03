"""Database connection layer — reads from Parquet, never locks DuckDB.

Architecture
------------
cpe_analytics.duckdb is the source of truth. dbt writes it. A
separate export step (scripts/export_parquet.py) dumps every table and
view into data/parquet/{schema}/{table}.parquet.

Streamlit opens an IN-MEMORY DuckDB connection and registers each
Parquet file as a view named `{schema}.{table}`. Because the in-memory
engine never opens cpe_analytics.duckdb, the file is never locked, so
dbt, scripts, and the analyst can use it freely while the dashboard is
running — the exact problem the previous architecture hit on Windows.

Writes from the UI (save_and_sync below) go through a short-lived
writer connection to the DuckDB file (safe, because Streamlit no
longer holds it open), then re-export the affected table to Parquet
and clear the Streamlit caches so the next query picks up fresh data.
"""
import sys
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "cpe_analytics.duckdb"
PARQUET_DIR = ROOT / "data" / "parquet"
SEED_DIR = ROOT / "dbt" / "seeds"

# Module-level tracking for the in-memory view catalog.
#   _registered_views maps "schema.table" → absolute parquet path as it was
#   last registered. If a parquet file is replaced (e.g. same path) the
#   mtime check catches it; if moved, the path check catches it.
#   _last_view_scan_mtime is the max mtime of any parquet under
#   data/parquet/ observed during the last successful scan. query() uses
#   it to skip the scan when nothing has changed on disk.
_registered_views: dict[str, str] = {}
_last_view_scan_mtime: float = 0.0


def _parquet_dir_latest_mtime() -> float:
    """Return the max mtime across every *.parquet under data/parquet/.

    Walk is cheap on the current tree (~160 files); measured sub-millisecond.
    Returns 0.0 when the directory is missing or empty so comparisons stay
    simple.
    """
    if not PARQUET_DIR.exists():
        return 0.0
    latest = 0.0
    for p in PARQUET_DIR.rglob("*.parquet"):
        try:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            continue
    return latest


def _register_parquet_views(conn: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """Bring the in-memory view catalog in sync with data/parquet/.

    Idempotent. Returns (added_or_updated, removed) counts. Uses
    CREATE OR REPLACE VIEW for new or path-changed files and
    DROP VIEW IF EXISTS for views whose backing file has been removed.
    Per-file errors are logged and skipped so one bad parquet cannot
    take down the whole scan.
    """
    added = 0
    removed = 0

    if not PARQUET_DIR.exists():
        # Directory vanished entirely — tear down any views we had.
        for view_name in list(_registered_views):
            schema, table = view_name.split(".", 1)
            try:
                conn.execute(f'DROP VIEW IF EXISTS "{schema}"."{table}"')
                _registered_views.pop(view_name, None)
                removed += 1
            except Exception as e:  # pragma: no cover
                print(f"  [db] warning: could not drop {view_name}: {e}", file=sys.stderr)
        return added, removed

    # Build the authoritative "what should exist" set from the filesystem.
    on_disk: dict[str, str] = {}
    for schema_dir in sorted(PARQUET_DIR.iterdir()):
        if not schema_dir.is_dir():
            continue
        schema = schema_dir.name
        # Always ensure the schema exists — cheap, idempotent.
        try:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        except Exception as e:  # pragma: no cover
            print(f"  [db] warning: could not create schema {schema}: {e}", file=sys.stderr)
            continue
        for parquet_file in sorted(schema_dir.glob("*.parquet")):
            table = parquet_file.stem
            view_name = f"{schema}.{table}"
            on_disk[view_name] = parquet_file.as_posix()

    # Register new files + re-register any whose backing path has changed.
    for view_name, path in on_disk.items():
        if _registered_views.get(view_name) == path:
            continue
        schema, table = view_name.split(".", 1)
        try:
            conn.execute(
                f'CREATE OR REPLACE VIEW "{schema}"."{table}" AS '
                f"SELECT * FROM read_parquet('{path}')"
            )
            _registered_views[view_name] = path
            added += 1
        except Exception as e:
            print(f"  [db] warning: could not register {view_name}: {e}", file=sys.stderr)

    # Drop views whose parquet file no longer exists.
    for view_name in list(_registered_views):
        if view_name in on_disk:
            continue
        schema, table = view_name.split(".", 1)
        try:
            conn.execute(f'DROP VIEW IF EXISTS "{schema}"."{table}"')
            _registered_views.pop(view_name, None)
            removed += 1
        except Exception as e:
            print(f"  [db] warning: could not drop {view_name}: {e}", file=sys.stderr)

    return added, removed


@st.cache_resource
def get_connection() -> duckdb.DuckDBPyConnection:
    """Return a cached in-memory DuckDB connection with every Parquet file
    registered as `{schema}.{table}`.

    Never opens cpe_analytics.duckdb — the dashboard therefore never holds
    a file lock on it. The view catalog is kept fresh by _refresh_views(),
    which is invoked from query() on every call and mtime-gated.
    """
    global _last_view_scan_mtime
    conn = duckdb.connect(":memory:")
    added, removed = _register_parquet_views(conn)
    _last_view_scan_mtime = _parquet_dir_latest_mtime()
    print(
        f"  [db] initial parquet scan — {added} views registered "
        f"(latest mtime: {_last_view_scan_mtime:.0f})",
        file=sys.stderr,
    )
    return conn


def _refresh_views(conn: duckdb.DuckDBPyConnection) -> None:
    """Re-scan data/parquet/ and reconcile views, but only when the
    directory's latest mtime has advanced past our last scan. Steady-
    state cost is one rglob + max(mtime) — microseconds. When a new
    parquet appears (CLI dbt seed, end_of_task.py, manual export) the
    mtime jumps and we pay the full scan once.

    When views were actually added or dropped, also flush the
    `@st.cache_data` result cache on `query()` — otherwise callers
    within the TTL window keep seeing results computed against the
    old catalog (stale success from pre-drop, or a cached empty-DF
    error from pre-add). See RULE 30: view-catalog refresh and
    result-cache invalidation are two halves of one problem.

    RULE 27 extension (2026-04-18): Streamlit's file-watcher re-
    imports db.py on any source byte change (git renormalize, an
    editor save, etc.), which resets `_registered_views` and
    `_last_view_scan_mtime` to their module-level defaults —
    empty dict + 0.0. If `@st.cache_resource` preserved the
    underlying connection across that reload, we can find ourselves
    with module state "knows no views" but the connection actually
    has views. The reverse — connection with zero views but module
    state claiming it knows some — is also possible. Detect both
    drifts up front and force a full re-scan by clearing the mtime
    gate, so the rest of the function brings state back in sync.

    Silent on no-ops. Logs only when views were added or dropped,
    or when a drift recovery forces a scan.
    """
    global _last_view_scan_mtime
    # --- RULE 27 drift detection ---
    # Probe the connection's view catalog and compare against what the
    # module dict thinks it registered. If they disagree on emptiness,
    # module and connection drifted — force a full re-scan by zeroing
    # the mtime gate. Wrapped in try/except so a stale/closed connection
    # still falls through to the scan (which will rebuild from scratch).
    try:
        conn_has_views = bool(conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_type = 'VIEW' "
            "  AND table_schema LIKE 'main_%' LIMIT 1"
        ).fetchone())
    except Exception:
        conn_has_views = False
    if (not _registered_views) and conn_has_views:
        # Module was re-imported; connection survived. Resync.
        _last_view_scan_mtime = 0.0
        print(
            "  [db] drift detected — module state empty but connection "
            "has views; forcing full re-scan (RULE 27 extension).",
            file=sys.stderr,
        )
    elif _registered_views and not conn_has_views:
        # Connection was replaced; module state is stale. Resync.
        _registered_views.clear()
        _last_view_scan_mtime = 0.0
        print(
            "  [db] drift detected — connection has no views but module "
            "claimed it knew some; forcing full re-scan (RULE 27 extension).",
            file=sys.stderr,
        )

    latest = _parquet_dir_latest_mtime()
    if latest <= _last_view_scan_mtime:
        return
    added, removed = _register_parquet_views(conn)
    _last_view_scan_mtime = latest
    if added or removed:
        # `query` is defined after this function at module scope — resolved
        # at call time, so the lookup works. Fall back to clearing the
        # whole `st.cache_data` if anything goes wrong.
        try:
            query.clear()
            cache_note = "query cache cleared"
        except Exception:
            try:
                st.cache_data.clear()
                cache_note = "st.cache_data cleared (fallback)"
            except Exception:
                cache_note = "cache clear failed"
        print(
            f"  [db] parquet re-scan — +{added} views, -{removed} views "
            f"(mtime now {_last_view_scan_mtime:.0f}; {cache_note})",
            file=sys.stderr,
        )


@st.cache_data(ttl=60)
def query(sql: str) -> pd.DataFrame:
    """Execute SQL against the Parquet-backed in-memory database.

    Refreshes the view catalog first when new parquet files have landed
    since the last scan — keeps the dashboard in sync with CLI-driven
    writes (dbt seed, end_of_task.py) without a Streamlit restart. See
    RULE 27 in knowledge_rules.md.

    RULE 30: does NOT swallow exceptions. DuckDB catalog / syntax / IO
    errors propagate to the caller, who decides whether an empty result
    is tolerable (callers that need tolerance wrap this with their own
    try/except). Silent empty-DataFrame returns from an errored query
    amplified bugs into downstream KeyErrors for up to 60 seconds.
    """
    conn = get_connection()
    _refresh_views(conn)
    return conn.execute(sql).fetchdf()


def close_connection() -> None:
    """Clear every piece of view/catalog state, so the next
    `get_connection()` rebuilds everything from disk.

    Called from save_and_sync + the Deploy button's post-pipeline
    handler after fresh Parquet files have been written, and any
    other caller that needs a hard reset.

    RULE 27 (Extension 2026-04-18 #2): there are FOUR state
    locations that must be cleared together, not two:

      1. `@st.cache_resource` cache on `get_connection()` — the
         in-memory DuckDB connection object.
      2. `@st.cache_data` cache on `query()` — prior result DFs.
      3. `_registered_views` — module-level dict mapping
         "schema.table" → last registered parquet path.
      4. `_last_view_scan_mtime` — module-level float storing the
         mtime gate that `_refresh_views` short-circuits against.

    Missing 3 or 4 produces a silent drift: the next
    `get_connection()` builds an EMPTY connection but
    `_register_parquet_views` sees `_registered_views` already
    claims to know all 161 paths → `continue`s every file → fresh
    connection has zero views. Caught eventually by Hotfix 2
    drift-detection, but cleaner to prevent the drift at the
    signal source.
    """
    global _registered_views, _last_view_scan_mtime
    # Clear module bookkeeping FIRST so the next `get_connection()`
    # does a true fresh scan (no stale `continue` from path matches).
    _registered_views.clear()
    _last_view_scan_mtime = 0.0
    try:
        get_connection.clear()
    except Exception:
        pass
    try:
        st.cache_data.clear()
    except Exception:
        pass


def save_and_sync(
    csv_path,
    table_name: str,
    schema: str = "main_seeds",
) -> tuple[bool, str]:
    """Push a CSV into DuckDB AND refresh the corresponding Parquet file.

    Flow:
      1. Open cpe_analytics.duckdb in writer mode (safe — Streamlit's
         in-memory engine doesn't hold a handle on the file)
      2. CREATE OR REPLACE TABLE <schema>.<table_name> from the CSV
      3. COPY that table to data/parquet/<schema>/<table_name>.parquet
      4. Close the writer
      5. close_connection() so the next Streamlit query rebuilds its
         in-memory view over the new Parquet file

    Returns (ok, detail). On failure falls back to exporting the CSV
    straight to Parquet via a throwaway in-memory connection, so the
    dashboard still sees the new rows even if the DuckDB file is busy.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return False, f"CSV file not found: {csv_path}"

    schema_dir = PARQUET_DIR / schema
    schema_dir.mkdir(parents=True, exist_ok=True)
    parquet_path = schema_dir / f"{table_name}.parquet"

    # --- Primary path: write through DuckDB file + export Parquet ---
    writer = None
    try:
        writer = duckdb.connect(str(DB_PATH), read_only=False)
        df = pd.read_csv(csv_path)
        writer.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        writer.execute(f'DROP TABLE IF EXISTS "{schema}"."{table_name}"')
        writer.register("_sync_df", df)
        writer.execute(
            f'CREATE TABLE "{schema}"."{table_name}" AS SELECT * FROM _sync_df'
        )
        writer.unregister("_sync_df")
        writer.execute(
            f'COPY (SELECT * FROM "{schema}"."{table_name}") '
            f"TO '{parquet_path.as_posix()}' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        writer.close()
        writer = None

        close_connection()
        return True, f"{len(df):,} rows loaded and exported to Parquet"

    except Exception as primary_err:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass

        # --- Fallback path: write Parquet directly via in-memory DuckDB ---
        # If cpe_analytics.duckdb is temporarily busy (e.g. another process
        # holds a writer), we still want the dashboard to see the CSV
        # changes. Writing straight to Parquet bypasses the file lock but
        # leaves cpe_analytics.duckdb stale until the next export run.
        try:
            tmp = duckdb.connect(":memory:")
            df = pd.read_csv(csv_path)
            tmp.register("_df", df)
            tmp.execute(
                f"COPY (SELECT * FROM _df) TO '{parquet_path.as_posix()}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            tmp.close()
            close_connection()
            return (
                True,
                f"{len(df):,} rows exported to Parquet (DuckDB file skipped: {primary_err})",
            )
        except Exception as fallback_err:
            return (
                False,
                f"primary: {primary_err}\nfallback: {fallback_err}",
            )


def validate_test_sql(test_sql: str):
    """Execute a dbt singular test SQL against the in-memory engine and
    return the violation count.

    Resolves `{{ ref('<model>') }}` jinja tokens by probing the known
    schemas (main_marts, main_obt, main_knowledge, main_vault,
    main_staging) until one resolves. Wraps the test as
    `SELECT COUNT(*) FROM (<sql>) _violations`.

    Returns `(ok, violation_count, detail)`.
    """
    import re

    if not test_sql or not test_sql.strip():
        return False, -1, "empty test SQL"

    conn = get_connection()
    exec_sql = test_sql

    refs = re.findall(r"\{\{\s*ref\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}", exec_sql)
    for ref_name in set(refs):
        resolved = None
        for schema in (
            "main_marts", "main_obt", "main_knowledge", "main_vault", "main_staging",
        ):
            try:
                conn.execute(f'SELECT 1 FROM "{schema}"."{ref_name}" LIMIT 0').fetchone()
                resolved = f'"{schema}"."{ref_name}"'
                break
            except Exception:
                continue
        if not resolved:
            return False, -1, f"could not resolve ref('{ref_name}') in any schema"
        exec_sql = re.sub(
            r"\{\{\s*ref\(\s*['\"]" + re.escape(ref_name) + r"['\"]\s*\)\s*\}\}",
            resolved,
            exec_sql,
        )

    try:
        count = conn.execute(
            f"SELECT COUNT(*) FROM ({exec_sql}) _dq_violations"
        ).fetchone()[0]
        return True, int(count), f"{int(count):,} violating rows"
    except Exception as e:
        return False, -1, str(e)
