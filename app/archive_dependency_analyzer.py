"""KI #71 — analyse the impact of archiving a business term.

Read-only data layer. Produces an :class:`ArchiveImpact` describing
what would be moved, what blocks the archive, and the guided-unwind
data the UI needs. The saga in ``archive_term.py`` calls this BEFORE
any file move; on any blocker it refuses to proceed.

Two blocker classes
-------------------
1. **SharingBlocker** — a model listed in this term's ``s2t_mapping``
   is also listed in another non-archived term's ``s2t_mapping``.
   Moving it would break that term's deploy.
2. **DownstreamBlocker** — per ``target/manifest.json``, a non-archived
   downstream model ``ref()``s a model we're about to move, and that
   downstream isn't itself slated for archive. Moving the upstream
   would break ``dbt compile``.

Algorithm (Q2 refinement)
-------------------------
The analyser does **not** auto-cascade beyond the term's own
``s2t_mapping`` targets. It only ever moves models the term explicitly
claims to own. Downstream impact is detected and reported, not
swallowed by silent cascading.

Manifest freshness gate (Q1)
----------------------------
* Stat ``dbt/target/manifest.json`` vs newest ``.sql`` under
  ``dbt/models/`` (excluding ``archive/``).
* If the manifest is newer than every model file AND contains every
  target model we care about → use as-is (the dominant case after
  ``end_of_task.py``).
* Otherwise → run ``dbt compile`` (~15-30s) then load.

Test injection
--------------
Every public function accepts optional ``manifest`` / ``glossary_df`` /
``s2t_df`` keyword arguments. Tests pass all three; production passes
none and the canonical paths are read.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
SEED_DIR = PROJECT_ROOT / "dbt" / "seeds"
DBT_DIR = PROJECT_ROOT / "dbt"
DBT_MODELS = DBT_DIR / "models"
ARCHIVE_ROOT = DBT_MODELS / "archive"
MANIFEST_PATH = DBT_DIR / "target" / "manifest.json"
GLOSSARY_CSV = SEED_DIR / "business_glossary.csv"
S2T_CSV = SEED_DIR / "s2t_mapping.csv"

DBT_COMPILE_TIMEOUT_SECONDS = 180


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TermRef:
    """A reference to another term, for blocker reporting + guided unwind."""
    term_id: str
    term_name: str
    status: str


@dataclass(frozen=True)
class SharingBlocker:
    """A target model also owned by another non-archived term."""
    model_name: str
    other_terms: tuple[TermRef, ...]


@dataclass(frozen=True)
class DownstreamBlocker:
    """A non-archived downstream model ``ref()``s a model we'd move."""
    model_name: str                       # the term's own target_model
    downstream_model: str                 # the consumer that ref()s it
    downstream_terms: tuple[TermRef, ...] # terms whose s2t_mapping owns the downstream


@dataclass
class ArchiveImpact:
    """Full impact summary. Consumed by the UI preview + the saga gate."""
    term_id: str
    term_name: str
    term_status: str
    target_models: list[str]
    sharing_blockers: list[SharingBlocker] = field(default_factory=list)
    downstream_blockers: list[DownstreamBlocker] = field(default_factory=list)

    @property
    def already_archived(self) -> bool:
        return self.term_status == "archived"

    @property
    def can_archive(self) -> bool:
        if self.already_archived:
            return False
        return not self.sharing_blockers and not self.downstream_blockers

    @property
    def exclusive_cascade(self) -> list[str]:
        """Models that WILL be moved if archive proceeds.

        Equals ``target_models`` when ``can_archive`` is True;
        empty list when blocked or already archived.
        """
        return list(self.target_models) if self.can_archive else []

    @property
    def all_blocking_terms(self) -> list[TermRef]:
        """Flat de-duplicated list of every other term involved in a
        blocker — what the guided-unwind UI iterates over."""
        seen: dict[str, TermRef] = {}
        for sb in self.sharing_blockers:
            for t in sb.other_terms:
                seen.setdefault(t.term_id, t)
        for db in self.downstream_blockers:
            for t in db.downstream_terms:
                seen.setdefault(t.term_id, t)
        return list(seen.values())


# ---------------------------------------------------------------------------
# Manifest freshness + loading
# ---------------------------------------------------------------------------

def _newest_model_mtime() -> float:
    """Max mtime of every ``.sql`` under ``dbt/models/`` excluding
    ``dbt/models/archive/``. Cheap walk — measured sub-millisecond
    on this tree.

    Returns 0.0 when the directory is missing.
    """
    if not DBT_MODELS.exists():
        return 0.0
    latest = 0.0
    for p in DBT_MODELS.rglob("*.sql"):
        try:
            p.relative_to(ARCHIVE_ROOT)
            continue  # skip archived files
        except ValueError:
            pass
        try:
            m = p.stat().st_mtime
            if m > latest:
                latest = m
        except OSError:
            continue
    return latest


def _manifest_contains_models(manifest: dict, model_names: list[str]) -> bool:
    """True iff every name in ``model_names`` resolves to a model node
    in the manifest. Used to defend against the just-Deploy'd-without-
    compile case where the manifest is fresh by mtime but stale by
    content."""
    if not model_names:
        return True
    present: set[str] = set()
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") == "model":
            n = node.get("name")
            if n:
                present.add(n)
    return all(m in present for m in model_names)


def _is_manifest_fresh(manifest_path: Path, target_models: list[str]) -> bool:
    """Combined mtime + content check."""
    if not manifest_path.exists():
        return False
    try:
        manifest_mtime = manifest_path.stat().st_mtime
    except OSError:
        return False
    if manifest_mtime < _newest_model_mtime():
        return False
    if target_models:
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not _manifest_contains_models(data, target_models):
            return False
    return True


def _run_dbt_compile() -> None:
    """Run ``dbt compile`` from the dbt project dir. Raises with a
    clear message on failure (RULE 30 — never swallow)."""
    dbt_exe = str(
        Path(sys.executable).parent / ("dbt.EXE" if os.name == "nt" else "dbt")
    )
    try:
        subprocess.run(
            [dbt_exe, "compile", "--threads", "1"],
            check=True,
            capture_output=True,
            text=True,
            cwd=str(DBT_DIR),
            timeout=DBT_COMPILE_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"dbt compile failed (exit {e.returncode}). "
            f"Cannot analyse archive impact without a current manifest.\n"
            f"stderr:\n{e.stderr or '(empty)'}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"dbt compile timed out after {DBT_COMPILE_TIMEOUT_SECONDS}s."
        ) from e


def _load_manifest(target_models: list[str], *, compile_if_stale: bool = True) -> dict:
    """Return a parsed manifest dict. Compiles on demand when stale or
    missing, unless ``compile_if_stale`` is False (tests pass it
    explicitly to assert the freshness path was hit)."""
    if _is_manifest_fresh(MANIFEST_PATH, target_models):
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    if not compile_if_stale:
        raise RuntimeError(
            f"manifest.json at {MANIFEST_PATH} is stale or missing, and "
            f"compile_if_stale=False was passed. Refusing to proceed."
        )

    _run_dbt_compile()

    if not MANIFEST_PATH.exists():
        raise RuntimeError(
            f"dbt compile completed but {MANIFEST_PATH} was not produced. "
            "Check the dbt configuration."
        )
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Manifest walking — reverse-ref graph
# ---------------------------------------------------------------------------

def _model_unique_id(manifest: dict, model_name: str) -> Optional[str]:
    """Return the dbt unique_id for a model by name, or None."""
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") == "model" and node.get("name") == model_name:
            return uid
    return None


def get_downstream_models(model_name: str, manifest: dict) -> list[str]:
    """All model names that ``ref()`` this model.

    A model "downstream" of X means X is in its ``depends_on.nodes``.
    Returns plain model names (not unique_ids), sorted for determinism.
    Returns [] if the model isn't in the manifest (likely just-deployed,
    not yet compiled) — caller may want to force a recompile.
    """
    uid = _model_unique_id(manifest, model_name)
    if uid is None:
        return []
    downstreams: list[str] = []
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "model":
            continue
        upstream = node.get("depends_on", {}).get("nodes", []) or []
        if uid in upstream:
            n = node.get("name")
            if n:
                downstreams.append(n)
    return sorted(set(downstreams))


# ---------------------------------------------------------------------------
# s2t_mapping + business_glossary lookups
# ---------------------------------------------------------------------------

def _load_glossary() -> pd.DataFrame:
    return pd.read_csv(GLOSSARY_CSV, keep_default_na=False, na_filter=False, dtype=str)


def _load_s2t() -> pd.DataFrame:
    return pd.read_csv(S2T_CSV, keep_default_na=False, na_filter=False, dtype=str)


def get_term_target_models(
    term_id: str,
    *,
    s2t_df: Optional[pd.DataFrame] = None,
) -> list[str]:
    """Distinct, non-empty target_models for this term."""
    df = s2t_df if s2t_df is not None else _load_s2t()
    term_rows = df[df["business_term_id"] == term_id]
    models = {
        str(m).strip()
        for m in term_rows["target_model"].tolist()
        if str(m).strip()
    }
    return sorted(models)


def terms_using_model(
    model_name: str,
    exclude_term_id: Optional[str] = None,
    *,
    s2t_df: Optional[pd.DataFrame] = None,
    glossary_df: Optional[pd.DataFrame] = None,
) -> list[TermRef]:
    """Non-archived terms whose ``s2t_mapping`` targets ``model_name``,
    excluding ``exclude_term_id`` if provided. Sorted by term_id for
    determinism."""
    s2t = s2t_df if s2t_df is not None else _load_s2t()
    gloss = glossary_df if glossary_df is not None else _load_glossary()

    matching = s2t[s2t["target_model"] == model_name]
    if exclude_term_id is not None:
        matching = matching[matching["business_term_id"] != exclude_term_id]
    candidate_ids = sorted(set(matching["business_term_id"].tolist()))
    if not candidate_ids:
        return []

    gloss_index = gloss.set_index("id")
    refs: list[TermRef] = []
    for tid in candidate_ids:
        if tid not in gloss_index.index:
            continue
        row = gloss_index.loc[tid]
        # In the corner case of duplicate ids (shouldn't happen but defensive),
        # pandas returns a DataFrame instead of a Series; take the first.
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        status = str(row.get("status", "")).strip() or "active"
        if status == "archived":
            continue
        refs.append(TermRef(
            term_id=tid,
            term_name=str(row.get("term_name", "")).strip(),
            status=status,
        ))
    return refs


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def analyze_archive_impact(
    term_id: str,
    *,
    manifest: Optional[dict] = None,
    glossary_df: Optional[pd.DataFrame] = None,
    s2t_df: Optional[pd.DataFrame] = None,
    compile_if_stale: bool = True,
) -> ArchiveImpact:
    """Produce the full impact analysis for archiving ``term_id``.

    Parameters
    ----------
    term_id
        e.g. ``"BG011"``.
    manifest, glossary_df, s2t_df
        Optional test injection. Pass all three to skip the filesystem
        and subprocess entirely. In production all default to None and
        canonical paths are read.
    compile_if_stale
        Production default True. Tests pass False to assert the
        freshness path was hit without actually compiling.

    Returns
    -------
    ArchiveImpact
        Includes ``can_archive`` (bool), the exclusive cascade list,
        and both blocker classes with guided-unwind data.

    Raises
    ------
    ValueError
        If ``term_id`` is not in ``business_glossary``.
    RuntimeError
        If the manifest is stale and ``dbt compile`` fails.
    """
    gloss = glossary_df if glossary_df is not None else _load_glossary()
    s2t = s2t_df if s2t_df is not None else _load_s2t()

    term_rows = gloss[gloss["id"] == term_id]
    if term_rows.empty:
        raise ValueError(f"Term {term_id!r} not found in business_glossary.")
    term_row = term_rows.iloc[0]
    term_name = str(term_row.get("term_name", "")).strip()
    term_status = str(term_row.get("status", "")).strip() or "active"

    target_models = get_term_target_models(term_id, s2t_df=s2t)

    if term_status == "archived":
        # No analysis needed — saga will no-op + info toast.
        return ArchiveImpact(
            term_id=term_id,
            term_name=term_name,
            term_status=term_status,
            target_models=target_models,
        )

    if not target_models:
        # Term exists but has nothing deployed yet — vacuous archive,
        # no blockers possible.
        return ArchiveImpact(
            term_id=term_id,
            term_name=term_name,
            term_status=term_status,
            target_models=[],
        )

    # Sharing blockers — no manifest needed.
    sharing: list[SharingBlocker] = []
    for model in target_models:
        others = terms_using_model(
            model, exclude_term_id=term_id, s2t_df=s2t, glossary_df=gloss,
        )
        if others:
            sharing.append(SharingBlocker(
                model_name=model,
                other_terms=tuple(others),
            ))

    # Downstream blockers — manifest required.
    mf = manifest if manifest is not None else _load_manifest(
        target_models, compile_if_stale=compile_if_stale,
    )

    target_set = set(target_models)
    downstream: list[DownstreamBlocker] = []
    for model in target_models:
        for ds in get_downstream_models(model, mf):
            if ds in target_set:
                continue  # downstream is also being moved by this archive
            # Is the downstream consumed by any other non-archived term?
            # That's the term the user must unwind first.
            ds_owners = terms_using_model(
                ds, exclude_term_id=term_id, s2t_df=s2t, glossary_df=gloss,
            )
            # Even if no term owns the downstream in s2t_mapping, it is
            # still a downstream blocker — the dbt compile graph would
            # break. The "owners" list may be empty in that case; the UI
            # still surfaces the model name so the user knows what to
            # resolve (manual review / delete the downstream).
            downstream.append(DownstreamBlocker(
                model_name=model,
                downstream_model=ds,
                downstream_terms=tuple(ds_owners),
            ))

    return ArchiveImpact(
        term_id=term_id,
        term_name=term_name,
        term_status=term_status,
        target_models=target_models,
        sharing_blockers=sharing,
        downstream_blockers=downstream,
    )
