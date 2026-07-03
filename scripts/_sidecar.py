"""Shared helper for writing artifact sidecar JSON files + supporting
hash/time computations.

Sidecars record what inputs an artifact was built against, enabling
staleness detection. All functions raise cleanly on error — no silent
fallbacks. Callers decide fallback semantics.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"


def now_iso_utc() -> str:
    """ISO 8601 UTC timestamp, second precision, Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def current_git_head_sha() -> str:
    """7-char short SHA of HEAD. Returns 'unknown' on any git failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=ROOT, check=True, timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def compute_file_hash(path: Path) -> str:
    """Full-content SHA-256 hex of a file. Reads in 64KB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_duckdb_content_hash(conn, schema: str, table: str) -> str:
    """md5(string_agg(t::TEXT ORDER BY t::TEXT)).

    Uses DuckDB-native md5 (fast, stable across restarts). Value is a
    content-equality token — not a cryptographic proof, just a detector
    for change. Quotes schema and table identifiers defensively.
    """
    sql = (
        f"SELECT md5(string_agg(t::TEXT, ',' ORDER BY t::TEXT)) "
        f'FROM "{schema}"."{table}" t'
    )
    row = conn.execute(sql).fetchone()
    return row[0] if row and row[0] else ""


def compute_duckdb_row_count(conn, schema: str, table: str) -> int:
    """COUNT(*). Used by the parquet sidecar (row_count
    only, no content hash, because parquet export is unconditional)."""
    row = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()
    return int(row[0]) if row else 0


def write_sidecar(artifact_name: str, payload: dict) -> None:
    """Atomically write logs/<artifact_name>_built_against.json.

    Write-to-tempfile + os.replace(). Creates logs/ if missing. Raises
    on failure. No post-write editing permitted (sidecars
    are written by their owning script, once per build).
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    target = LOGS_DIR / f"{artifact_name}_built_against.json"

    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{artifact_name}_sidecar_", suffix=".tmp", dir=str(LOGS_DIR)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def compute_dbt_sql_tree_hash(dbt_models_dir: Path) -> tuple[str, list[str]]:
    """Deterministic tree hash over all non-archived dbt model SQL files.

    Replaces content-hashing of the scanner outputs (dbt_column_lineage.csv,
    dbt_model_catalog.csv), which rotate non-deterministically across runs
    (per-run timestamp + chain-dependent plain-description churn). The SQL
    tree itself IS the upstream-stable signal: if no .sql file under
    dbt/models/ changed, any derived scanner output is reproducible from
    that input, so wiki freshness can be gated on the tree alone.

    Returns (tree_hash, sorted_paths) where:
    - tree_hash: sha256 hex of sorted "relative_path:file_sha256" lines.
    - sorted_paths: list of repo-relative paths (forward slashes) in sort
      order, for inclusion in sidecar metadata.

    Exclusions:
    - Any path containing "/archive/" (or "\\archive\\" on Windows).
    - Non-.sql files (.yml, .jinja, .md, etc.).
    """
    # Resolve to absolute so relative_to(ROOT) works regardless of caller's cwd
    # or whether caller passed a relative or absolute dbt_models_dir.
    abs_dir = dbt_models_dir.resolve()
    entries: list[tuple[str, str]] = []
    for sql_path in sorted(abs_dir.rglob("*.sql")):
        # Cross-platform archive exclusion: check path parts.
        if "archive" in sql_path.parts:
            continue
        rel = sql_path.relative_to(ROOT).as_posix()
        entries.append((rel, compute_file_hash(sql_path)))

    payload = "\n".join(f"{p}:{h}" for p, h in entries)
    tree_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return tree_hash, [p for p, _ in entries]


def overall_hash_from_inputs(per_input_hashes: dict[str, str]) -> str:
    """Deterministic sha256 over concatenated per-input hashes, sorted by key.

    Used by wiki + context sidecars. Parquet sidecar uses a row_count-based
    overall hash computed inline.
    """
    payload = "\n".join(f"{k}={per_input_hashes[k]}" for k in sorted(per_input_hashes))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------
# Staleness detection (read flow)
# ---------------------------------------------------------------------

SIDECAR_SCHEMA_VERSION = 1


def _load_sidecar(artifact_name: str) -> tuple[dict | None, str | None]:
    """Read sidecar JSON. Returns (payload, error_reason).
    payload is None when error_reason is set."""
    path = LOGS_DIR / f"{artifact_name}_built_against.json"
    if not path.exists():
        return None, "sidecar missing, first run or artifact never built"
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception as e:
        return None, f"sidecar malformed ({type(e).__name__}: {e})"
    if payload.get("schema_version") != SIDECAR_SCHEMA_VERSION:
        return None, (
            f"sidecar schema_version {payload.get('schema_version')!r} "
            f"!= supported {SIDECAR_SCHEMA_VERSION}"
        )
    return payload, None


def _stale_result(artifact: str, reason: str, evidence: dict | None = None) -> dict:
    return {"artifact": artifact, "is_stale": True, "reason": reason,
            "evidence": evidence or {}}


def _fresh_result(artifact: str) -> dict:
    return {"artifact": artifact, "is_stale": False, "reason": "", "evidence": {}}


def check_artifact_staleness(artifact_name: str) -> dict:
    """Compare stored sidecar against current input state.

    Returns {artifact, is_stale, reason, evidence}.
    - artifact: the name passed in.
    - is_stale: True when current state differs from stored sidecar OR
      when the sidecar is missing/malformed/wrong schema_version.
    - reason: human-readable explanation when stale; empty string when fresh.
    - evidence: structured detail of what changed (for UI / run log).
    """
    payload, err = _load_sidecar(artifact_name)
    if err:
        return _stale_result(artifact_name, err)

    stored_inputs = payload.get("inputs", {})
    if artifact_name == "wiki":
        return _check_wiki(stored_inputs)
    if artifact_name == "context":
        return _check_context(stored_inputs)
    if artifact_name == "parquet":
        return _check_parquet(stored_inputs)
    if artifact_name == "source_column_roles":
        return _check_source_column_roles(stored_inputs)
    raise ValueError(f"unknown artifact: {artifact_name!r}")


def _check_wiki(stored_inputs: dict) -> dict:
    """Wiki sidecar stores per-CSV sha256 for stable seeds + an upstream
    dbt_models_sql_tree hash (replaces scanner-seed tracking).

    Old-format sidecars (no dbt_models_sql_tree key) are
    treated as outdated → force-rebuild.
    """
    if "dbt_models_sql_tree" not in stored_inputs:
        return _stale_result(
            "wiki",
            "sidecar schema outdated (no dbt_models_sql_tree key) — force rebuild",
        )

    changed: list[str] = []
    removed: list[str] = []

    # Compare per-seed sha256s against current disk content.
    for key, rec in stored_inputs.items():
        if key == "dbt_models_sql_tree":
            continue  # handled below
        csv_path = ROOT / "dbt" / "seeds" / key
        if not csv_path.exists():
            removed.append(key)
            continue
        if compute_file_hash(csv_path) != rec.get("sha256"):
            changed.append(key)

    # Compare dbt model SQL tree hash.
    tree_rec = stored_inputs["dbt_models_sql_tree"]
    current_tree_hash, _ = compute_dbt_sql_tree_hash(ROOT / "dbt" / "models")
    tree_changed = current_tree_hash != tree_rec.get("tree_hash")

    if not (changed or removed or tree_changed):
        return _fresh_result("wiki")

    reasons: list[str] = []
    if changed:
        reasons.append(f"{len(changed)} seeds modified: {', '.join(sorted(changed))}")
    if removed:
        reasons.append(f"seeds removed: {', '.join(sorted(removed))}")
    if tree_changed:
        reasons.append("dbt model SQL tree changed")

    return _stale_result(
        "wiki", "; ".join(reasons),
        {
            "changed": sorted(changed),
            "removed": sorted(removed),
            "tree_changed": tree_changed,
        },
    )


def _check_context(stored_inputs: dict) -> dict:
    """Context sidecar stores per-table content_sha256. Recompute + compare."""
    import duckdb

    db_path = ROOT / "cpe_analytics.duckdb"
    if not db_path.exists():
        return _stale_result("context", "cpe_analytics.duckdb missing")
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        changed: list[str] = []
        for qn, rec in stored_inputs.items():
            schema, _, table = qn.partition(".")
            try:
                current = compute_duckdb_content_hash(conn, schema, table)
            except Exception as e:
                changed.append(f"{qn} (query error: {type(e).__name__})")
                continue
            if current != rec.get("content_sha256"):
                changed.append(qn)
    finally:
        conn.close()
    if not changed:
        return _fresh_result("context")
    return _stale_result(
        "context",
        f"{len(changed)} context inputs changed: {', '.join(sorted(changed))}",
        {"changed": sorted(changed)},
    )


def _check_source_column_roles(stored_inputs: dict) -> dict:
    """Source-column-roles sidecar stores sha256 of both seed CSVs. Fresh
    when on-disk sha256 matches stored."""
    changed: list[str] = []
    for name in ("source_column_roles.csv", "source_column_role_changes.csv"):
        path = ROOT / "dbt" / "seeds" / name
        stored = (stored_inputs.get(name) or {}).get("sha256")
        if not path.exists():
            changed.append(f"{name} missing")
            continue
        if compute_file_hash(path) != stored:
            changed.append(name)
    if not changed:
        return _fresh_result("source_column_roles")
    return _stale_result(
        "source_column_roles",
        f"{len(changed)} input(s) changed: {', '.join(sorted(changed))}",
        {"changed": sorted(changed)},
    )


def _check_parquet(stored_inputs: dict) -> dict:
    """Parquet sidecar stores per-table row_count only. Recompute + compare.
    Parquet is forensic-only — rebuild is unconditional elsewhere. This check
    exists for the warning message; it does not skip-gate export."""
    import duckdb

    db_path = ROOT / "cpe_analytics.duckdb"
    if not db_path.exists():
        return _stale_result("parquet", "cpe_analytics.duckdb missing")
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        changed: list[str] = []
        for qn, rec in stored_inputs.items():
            schema, _, table = qn.partition(".")
            try:
                current = compute_duckdb_row_count(conn, schema, table)
            except Exception as e:
                changed.append(f"{qn} (query error: {type(e).__name__})")
                continue
            if current != rec.get("row_count"):
                changed.append(f"{qn} (was {rec.get('row_count')}, now {current})")
    finally:
        conn.close()
    if not changed:
        return _fresh_result("parquet")
    # Truncate noise in the warning message
    displayed = sorted(changed)[:5]
    tail = f" (+{len(changed) - 5} more)" if len(changed) > 5 else ""
    return _stale_result(
        "parquet",
        f"{len(changed)} parquet tables have row count drift: "
        f"{', '.join(displayed)}{tail}",
        {"changed": sorted(changed)},
    )
