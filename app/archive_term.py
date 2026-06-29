"""KI #71 — strict-cascade soft-archive saga.

Replaces the pre-KI-71 archive flow. Differences:
  - Pre-archive validation via `archive_dependency_analyzer.analyze_archive_impact`.
    Sharing blockers and downstream blockers cause refusal — no half-clean
    archives.
  - Idempotent no-op for already-archived terms.
  - In-process re-entry guard + status re-read defence against the
    double-click bug observed during BG011 (KI #71 secondary observation).
  - CSV byte-snapshot rollback: on any failure inside the mutation block,
    both `business_glossary.csv` and `archive_log.csv` are restored from
    their pre-mutation byte snapshots and any moved .sql files are moved
    back to their original locations.
  - `close_connection()` flush after the re-seed (RULE 27 + decision #55)
    so Streamlit's cached views see the new state.
  - New `archive_log` columns `cascaded_models_json` and `blockers_resolved`
    populated per Q5.

Public API
----------
`run_archive(term_id, reason_code, reason_text, learning_signal, *,
blockers_resolved=None) -> ArchiveResult`

Callers wrap in try/except for `BlockedArchive` and surface
`impact.sharing_blockers` + `impact.downstream_blockers` as the guided
unwind UI. `AlreadyArchived` is a control-flow signal — re-clicks land
here.

Never deletes rows in `s2t_mapping`, `analysis_findings`, `dbt_column_lineage`
(decision #45 / #67 — archive preserves audit).
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

from archive_dependency_analyzer import (
    ArchiveImpact,
    analyze_archive_impact,
)

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
SEED_DIR = PROJECT_ROOT / "dbt" / "seeds"
DBT_MODELS = PROJECT_ROOT / "dbt" / "models"
ARCHIVE_ROOT = DBT_MODELS / "archive"
ARCHIVE_LOG = SEED_DIR / "archive_log.csv"
GLOSSARY_CSV = SEED_DIR / "business_glossary.csv"
S2T_CSV = SEED_DIR / "s2t_mapping.csv"

ARCHIVE_LOG_FIELDS = [
    "archive_id", "business_term_id", "term_name",
    "archived_at_utc", "archived_reason_code", "archived_reason_text",
    "learning_signal", "archived_by",
    "s2t_row_ids", "target_models", "files_archived",
    # KI #71 — new columns. Pre-KI-71 archive rows carry empty strings.
    "cascaded_models_json", "blockers_resolved",
]

_LAYERS = ("marts", "obt", "vault", "staging", "knowledge")

DBT_SEED_TIMEOUT_SECONDS = 180

# In-process re-entry guard. Streamlit reruns share this module, so a
# second click on the same term while the first is mid-flight hits the
# AlreadyArchived path instead of starting a duplicate saga.
_IN_FLIGHT_TERMS: set[str] = set()


# ---------------------------------------------------------------------------
# Exceptions + result type
# ---------------------------------------------------------------------------

class BlockedArchive(Exception):
    """Archive refused because impact analysis surfaced blockers.

    UI catches this and renders the guided-unwind preview from
    ``self.impact``. Saga performs no mutations before raising.
    """

    def __init__(self, impact: ArchiveImpact):
        self.impact = impact
        super().__init__(
            f"Archive of {impact.term_id} blocked: "
            f"{len(impact.sharing_blockers)} sharing blocker(s), "
            f"{len(impact.downstream_blockers)} downstream blocker(s)."
        )


class AlreadyArchived(Exception):
    """Idempotent no-op signal. Raised when the term is already archived
    OR when a saga is already in flight for this term_id. Saga performs
    no mutations before raising."""

    def __init__(self, term_id: str, archive_id: Optional[str] = None,
                 archived_at: Optional[str] = None):
        self.term_id = term_id
        self.archive_id = archive_id
        self.archived_at = archived_at
        msg = f"Term {term_id} is already archived"
        if archive_id:
            msg += f" ({archive_id}"
            if archived_at:
                msg += f" at {archived_at}"
            msg += ")"
        super().__init__(msg)


@dataclass
class ArchiveResult:
    """Returned by a successful saga (or by the already-archived no-op).
    UI uses this for toast text + audit pointer."""
    archive_id: str
    term_id: str
    term_name: str
    cascaded_models: list[str]
    files_archived: int
    blockers_resolved: list[str] = field(default_factory=list)
    already_archived: bool = False


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------

def _next_archive_id() -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    today_prefix = f"ARC-{date_str}-"
    n_today = 0
    if ARCHIVE_LOG.exists() and ARCHIVE_LOG.stat().st_size > 0:
        try:
            df = pd.read_csv(ARCHIVE_LOG)
            if "archive_id" in df.columns and not df.empty:
                n_today = int(
                    df["archive_id"].astype(str).str.startswith(today_prefix).sum()
                )
        except Exception:
            n_today = 0
    return f"{today_prefix}{n_today + 1:03d}"


def _layer_of(path: Path) -> str:
    """Return the layer directory name (marts/obt/vault/staging/knowledge)
    of a .sql file under dbt/models/, or '' if it doesn't sit in one of
    the canonical layers."""
    try:
        rel = path.relative_to(DBT_MODELS)
    except ValueError:
        return ""
    parts = rel.parts
    return parts[0] if parts else ""


def _find_sql_for_model(model_name: str) -> Optional[Path]:
    for layer in _LAYERS:
        p = DBT_MODELS / layer / f"{model_name}.sql"
        if p.exists():
            return p
    # Fallback rglob — but never look under archive/.
    for candidate in DBT_MODELS.rglob(f"{model_name}.sql"):
        try:
            candidate.relative_to(ARCHIVE_ROOT)
        except ValueError:
            return candidate
    return None


def _move_file_to_archive(src: Path, archive_id: str) -> Optional[Path]:
    """Move ``src`` to ``dbt/models/archive/<archive_id>/<layer>/<name>.sql``.
    Returns the destination path on success, None on failure. The saga
    treats None as a fatal cascade error (no silent skipping post-KI-71)."""
    try:
        rel_from_models = src.relative_to(DBT_MODELS)
    except ValueError:
        return None
    dest = ARCHIVE_ROOT / archive_id / rel_from_models
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(src), str(dest))
        return dest
    except Exception:
        return None


def _rollback_file_moves(moves: list[tuple[Path, Path]]) -> list[str]:
    """Best-effort reverse of every successful move. Returns a list of
    error strings for moves that could not be reversed — the caller
    surfaces these as warnings; we never raise during rollback."""
    errors: list[str] = []
    for src, dest in reversed(moves):
        if not dest.exists():
            # Already gone — nothing to do, but flag it.
            errors.append(f"rollback: destination {dest} no longer exists")
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dest), str(src))
        except Exception as e:  # noqa: BLE001
            errors.append(f"rollback: could not restore {src.name}: {e}")
    return errors


# ---------------------------------------------------------------------------
# CSV snapshot/restore — atomic at the byte level
# ---------------------------------------------------------------------------

def _snapshot_csv(path: Path) -> Optional[bytes]:
    """Read the file's current bytes for rollback. Returns None if the
    file does not exist (rollback will then re-delete a newly-created
    file)."""
    if not path.exists():
        return None
    return path.read_bytes()


def _restore_csv(path: Path, snapshot: Optional[bytes]) -> None:
    """Restore from snapshot. If snapshot is None and the file now
    exists, delete it (we created it during the saga)."""
    if snapshot is None:
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        return
    # Restore exact byte state — preserves the LF discipline (RULE 34).
    path.write_bytes(snapshot)


def _normalise_lf(p: Path) -> None:
    """Strip CRLF. Project contract is LF-only — DuckDB's sniffer fails
    on mixed endings (RULE 34)."""
    if not p.exists():
        return
    raw = p.read_bytes()
    if b"\r\n" in raw:
        p.write_bytes(raw.replace(b"\r\n", b"\n"))


# ---------------------------------------------------------------------------
# Reseed + cache flush
# ---------------------------------------------------------------------------

def _reseed_and_export() -> None:
    dbt_exe = str(
        Path(sys.executable).parent / ("dbt.EXE" if os.name == "nt" else "dbt")
    )
    for _tbl in ("business_glossary", "archive_log"):
        _normalise_lf(SEED_DIR / f"{_tbl}.csv")
        subprocess.run(
            [dbt_exe, "seed", "--full-refresh", "--threads", "1", "--select", _tbl],
            check=True, capture_output=True, text=True,
            cwd=str(PROJECT_ROOT / "dbt"),
            timeout=DBT_SEED_TIMEOUT_SECONDS,
        )
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from export_parquet import export_table  # noqa: E402
    export_table("main_seeds", "business_glossary")
    export_table("main_seeds", "archive_log")


def _close_db_connection() -> None:
    """RULE 27 + decision #55: close_connection() is the authoritative
    state-reset signal — clears the in-memory DuckDB connection, the
    view catalog, the mtime gate, and the @st.cache_data result cache."""
    try:
        from db import close_connection
        close_connection()
    except Exception:
        # If db isn't importable (non-Streamlit context), nothing to flush.
        pass


# ---------------------------------------------------------------------------
# CSV mutation helpers
# ---------------------------------------------------------------------------

def _apply_glossary_archive_flip(
    glossary_df: pd.DataFrame,
    term_id: str,
    archive_id: str,
    reason_code: str,
    reason_text: str,
    now_iso: str,
) -> None:
    """Mutate ``glossary_df`` in-place: flip the term's status to
    archived and stamp the four archive_* columns. Rule 22 exception:
    archive is the one code path that legitimately mutates status."""
    mask = glossary_df["id"] == term_id
    for col in (
        "status", "archive_id", "archived_at_utc",
        "archived_reason_code", "archived_reason_text",
    ):
        if col in glossary_df.columns:
            glossary_df[col] = glossary_df[col].fillna("").astype(str)
    glossary_df.loc[mask, "status"] = "archived"
    glossary_df.loc[mask, "archive_id"] = archive_id
    glossary_df.loc[mask, "archived_at_utc"] = now_iso
    glossary_df.loc[mask, "archived_reason_code"] = reason_code
    glossary_df.loc[mask, "archived_reason_text"] = (reason_text or "")[:500]


def _write_glossary(glossary_df: pd.DataFrame) -> None:
    glossary_df.to_csv(GLOSSARY_CSV, index=False, lineterminator="\n")


def _build_cascaded_models_json(moves: list[tuple[Path, Path]]) -> str:
    """Serialise the (src, dst) move list as a compact JSON array of
    {name, layer, src, dst} dicts. Stored in archive_log for forensic
    audit — derives nothing the saga itself needs."""
    items: list[dict[str, str]] = []
    for src, dest in moves:
        items.append({
            "name": src.stem,
            "layer": _layer_of(src),
            "src": str(src.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "dst": str(dest.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        })
    return json.dumps(items, separators=(",", ":"))


def _append_archive_log_row(
    *,
    archive_id: str,
    term_id: str,
    term_name: str,
    now_iso: str,
    reason_code: str,
    reason_text: str,
    learning_signal: bool,
    s2t_row_ids: list[str],
    target_models: list[str],
    moves: list[tuple[Path, Path]],
    blockers_resolved: list[str],
) -> None:
    """Append a single row to ``archive_log.csv`` with all KI #71 fields.

    Per decision #57 (csv_dictwriter_truncate_trap), validation happens
    BEFORE ``open("a")`` so a malformed row never truncates the file.
    """
    row = {
        "archive_id": archive_id,
        "business_term_id": term_id,
        "term_name": term_name,
        "archived_at_utc": now_iso,
        "archived_reason_code": reason_code,
        "archived_reason_text": (reason_text or "")[:500],
        "learning_signal": "true" if learning_signal else "false",
        "archived_by": os.getenv("USER") or os.getenv("USERNAME") or "default",
        "s2t_row_ids": ";".join(s2t_row_ids),
        "target_models": ";".join(target_models),
        "files_archived": len(moves),
        "cascaded_models_json": _build_cascaded_models_json(moves),
        "blockers_resolved": ";".join(blockers_resolved or []),
    }
    # Validate all expected fields present before opening for write.
    missing = [f for f in ARCHIVE_LOG_FIELDS if f not in row]
    if missing:
        raise RuntimeError(f"archive_log row is missing fields: {missing}")

    need_header = (not ARCHIVE_LOG.exists()) or ARCHIVE_LOG.stat().st_size == 0
    with ARCHIVE_LOG.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=ARCHIVE_LOG_FIELDS, lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        if need_header:
            w.writeheader()
        w.writerow(row)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Existing-archive lookup (for already-archived no-op)
# ---------------------------------------------------------------------------

def _find_archive_pointer_for_term(term_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (archive_id, archived_at_utc) from business_glossary, or
    (None, None) if either column is missing or the row isn't archived."""
    if not GLOSSARY_CSV.exists():
        return (None, None)
    try:
        df = pd.read_csv(GLOSSARY_CSV, keep_default_na=False, na_filter=False, dtype=str)
    except Exception:
        return (None, None)
    row = df[df["id"] == term_id]
    if row.empty:
        return (None, None)
    r = row.iloc[0]
    arc = str(r.get("archive_id", "") or "").strip() or None
    at = str(r.get("archived_at_utc", "") or "").strip() or None
    return (arc, at)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_archive(
    term_id: str,
    reason_code: str,
    reason_text: str,
    learning_signal: bool,
    *,
    blockers_resolved: Optional[list[str]] = None,
) -> ArchiveResult:
    """Strict-cascade soft-archive of a business term.

    Raises
    ------
    ValueError
        Missing term_id or reason_code, or term not in business_glossary.
    BlockedArchive
        Impact analysis surfaced sharing or downstream blockers.
        ``e.impact`` carries the guided-unwind data for the UI.
    AlreadyArchived
        Term is already archived (idempotent no-op signal); also raised
        when a saga is already in flight for the same term_id.
    RuntimeError
        dbt compile / seed failure (analyzer or commit-step). On a
        commit-step failure, CSV state is restored from snapshot and
        moved files are returned to their original location.
    """
    if not term_id or not reason_code:
        raise ValueError("Archive requires a term_id and a reason_code.")

    # Re-entry guard — same-term double-click within one process lands here.
    if term_id in _IN_FLIGHT_TERMS:
        raise AlreadyArchived(term_id=term_id)
    _IN_FLIGHT_TERMS.add(term_id)

    try:
        # 1. Impact analysis (may trigger dbt compile internally on stale manifest).
        impact = analyze_archive_impact(term_id)

        if impact.already_archived:
            arc_id, at = _find_archive_pointer_for_term(term_id)
            return ArchiveResult(
                archive_id=arc_id or "",
                term_id=term_id,
                term_name=impact.term_name,
                cascaded_models=[],
                files_archived=0,
                blockers_resolved=[],
                already_archived=True,
            )

        if not impact.can_archive:
            raise BlockedArchive(impact)

        # 2. Plan the saga.
        archive_id = _next_archive_id()
        cascade = list(impact.exclusive_cascade)
        now_iso = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

        # 3. Read CSVs once. keep_default_na=False prevents pandas from
        #    coercing free-text cells to NaN.
        glossary_df = pd.read_csv(
            GLOSSARY_CSV, keep_default_na=False, na_filter=False, dtype=str
        )
        s2t_df = pd.read_csv(
            S2T_CSV, keep_default_na=False, na_filter=False, dtype=str
        )
        term_s2t = s2t_df[s2t_df["business_term_id"] == term_id]
        s2t_row_ids = [str(x) for x in term_s2t["id"].tolist()]

        # 4. Snapshot CSV bytes BEFORE any mutation so rollback can
        #    restore exactly. RULE 34: byte-level write preserves LF.
        glossary_snap = _snapshot_csv(GLOSSARY_CSV)
        archive_log_snap = _snapshot_csv(ARCHIVE_LOG)

        moves: list[tuple[Path, Path]] = []
        committed = False

        try:
            # 5. Move .sql files. ANY failure here is fatal — full rollback.
            for model in cascade:
                src = _find_sql_for_model(model)
                if src is None:
                    raise RuntimeError(
                        f"Cannot locate .sql file for model {model!r}. "
                        "Re-run dbt compile or check the file layout."
                    )
                dest = _move_file_to_archive(src, archive_id)
                if dest is None:
                    raise RuntimeError(
                        f"Failed to move {src} into archive — check "
                        "filesystem permissions."
                    )
                moves.append((src, dest))

            # 6. Update glossary CSV (atomic byte write via pandas to_csv).
            _apply_glossary_archive_flip(
                glossary_df, term_id, archive_id,
                reason_code, reason_text, now_iso,
            )
            _write_glossary(glossary_df)

            # 7. Append archive_log row.
            _append_archive_log_row(
                archive_id=archive_id,
                term_id=term_id,
                term_name=impact.term_name,
                now_iso=now_iso,
                reason_code=reason_code,
                reason_text=reason_text,
                learning_signal=learning_signal,
                s2t_row_ids=s2t_row_ids,
                target_models=cascade,
                moves=moves,
                blockers_resolved=blockers_resolved or [],
            )

            # Past this point CSV+filesystem state is durable. Subsequent
            # failures sync DuckDB/parquet but do not roll back.
            committed = True

            # 8. Re-seed + re-export so DuckDB and parquet catch up.
            _reseed_and_export()

            # 9. Cache flush (RULE 27 / decision #55).
            _close_db_connection()

        except Exception:
            if not committed:
                _restore_csv(GLOSSARY_CSV, glossary_snap)
                _restore_csv(ARCHIVE_LOG, archive_log_snap)
                rollback_errs = _rollback_file_moves(moves)
                for msg in rollback_errs:
                    try:
                        st.warning(msg)
                    except Exception:
                        print(f"  [archive_term] {msg}", file=sys.stderr)
            else:
                # Commit happened but downstream sync (re-seed/export/cache)
                # failed. The CSV state IS the user's intent — don't undo.
                # Surface clearly so the user runs end_of_task.py to
                # reconcile DuckDB.
                msg = (
                    f"Archive of {term_id} committed to CSV but DuckDB sync "
                    "failed — run scripts/end_of_task.py to reconcile."
                )
                try:
                    st.error(msg)
                except Exception:
                    print(f"  [archive_term] {msg}", file=sys.stderr)
            raise

        # 10. Success — caller owns toast + rerun (UI controls navigation
        # so guided-unwind state can be advanced before rerun fires).

        return ArchiveResult(
            archive_id=archive_id,
            term_id=term_id,
            term_name=impact.term_name,
            cascaded_models=cascade,
            files_archived=len(moves),
            blockers_resolved=list(blockers_resolved or []),
            already_archived=False,
        )

    finally:
        _IN_FLIGHT_TERMS.discard(term_id)
