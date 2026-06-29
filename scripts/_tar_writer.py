"""Piece 9 Stage C — Term Analysis Results (TAR) writer.

Writes Stage C run output to dbt/seeds/term_analysis_results.csv as one
atomic transaction per run. One run = N query rows (N >= 0) + exactly one
sufficiency row. Supersedes any prior-success rows for the term_id before
writing new ones, per design doc §28.11.3 + v5 Edit 12.

Supersede semantics (v4 D9):
  UPDATE ... SET status='superseded', superseded_by=<new sufficiency id>
  WHERE term_id=X AND status='success'

  New rows get fresh TAR-NNNNN ids and status='success'. Cross-term
  citations to superseded rows remain readable (Piece 8 loader follows
  them with a freshness annotation per v5 Edit 7).

After write: sync parquet + invalidate Streamlit view catalog via the
shared helper (known_issue #53 pattern — mirrors Stage A / Stage B
writers).

Atomic write discipline: all rows for a run are assembled in memory,
merged with existing CSV rows and supersede updates, then replaced
atomically via `.tmp` + `os.replace`. Partial writes cannot occur; a
mid-run Python crash leaves the CSV untouched.

Row construction helpers build_query_row and build_sufficiency_row
enforce the 19-column shape defined in schema.yml. The runner is
responsible for content (query_sql, interpretation, sufficiency_json,
etc.); the writer is responsible for persistence discipline.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
_TAR_CSV = _ROOT / "dbt" / "seeds" / "term_analysis_results.csv"

TAR_FIELDS = [
    "id", "term_id", "row_type", "analysis_lens", "stage", "query_index",
    "query_sql", "query_result_json", "result_row_count", "interpretation",
    "grounded_in_tar_ids", "sufficiency_json", "status", "confidence",
    "executed_at_utc", "executed_by", "superseded_by", "run_id",
    "llm_usage_json", "validation_errors_json", "error_message",
]

_ALLOWED_ROW_TYPES = frozenset({"query", "sufficiency"})
_ALLOWED_STATUSES = frozenset({"success", "error", "superseded", "quarantined"})
_ALLOWED_LENSES = frozenset({
    "measures_overview", "by_dimension", "ranking", "time_trend",
    "cumulative", "variance", "bucketing", "part_to_whole", "",
})
_ALLOWED_STAGES = frozenset({
    "framework_floor", "reflection", "sufficiency_loop", "terminal",
})


# ─── ID + timestamp helpers ─────────────────────────────────────────────

def _now_utc_naive() -> dt.datetime:
    """Tz-naive UTC, matching Stage A / Stage B conventions."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _next_tar_id(existing_rows: list[dict]) -> str:
    """Generate the next TAR-NNNNN id (5-digit zero-padded, sequential)."""
    max_n = 0
    for row in existing_rows:
        rid = row.get("id", "") or ""
        if rid.startswith("TAR-"):
            try:
                n = int(rid.split("-", 1)[1])
                if n > max_n:
                    max_n = n
            except (IndexError, ValueError):
                continue
    return f"TAR-{max_n + 1:05d}"


def build_run_id(term_id: str, at: Optional[dt.datetime] = None) -> str:
    """TARRUN-YYYYMMDDHHMMSS-<term_id>. Stamped identically on every row
    produced by a single Stage C run. Format preserves the term_id as
    the trailing segment so hyphenated ids (e.g. BG-T25-TERMEDA) parse
    by splitting on the first two hyphens.
    """
    at = at or _now_utc_naive()
    ts = at.strftime("%Y%m%d%H%M%S")
    return f"TARRUN-{ts}-{term_id}"


# ─── Row construction ────────────────────────────────────────────────────

def build_query_row(
    *,
    term_id: str,
    analysis_lens: str,
    stage: str,
    query_index: int,
    query_sql: str,
    query_result_json: str,
    result_row_count: int,
    interpretation: str,
    grounded_in_tar_ids: list[str],
    status: str = "success",
    llm_usage_json: str = "{}",
    error_message: str = "",
) -> dict:
    """Build a query-type TAR row dict. Validates enum fields + shape.

    `id`, `executed_at_utc`, `executed_by`, `run_id`, `superseded_by`
    are stamped by write_tar_run — not this helper. The row returned
    has placeholders for those.

    `error_message` (KI-113) carries the DuckDB error string when
    status='error'; empty otherwise. Surfaces to the LLM in the
    next-turn bundle so it can self-correct binder errors instead of
    blindly retrying. Mirrors Stage B's domain_analysis_results
    error_message convention.
    """
    if analysis_lens not in _ALLOWED_LENSES:
        raise ValueError(
            f"analysis_lens={analysis_lens!r} not in {_ALLOWED_LENSES}"
        )
    if stage not in _ALLOWED_STAGES:
        raise ValueError(f"stage={stage!r} not in {_ALLOWED_STAGES}")
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"status={status!r} not in {_ALLOWED_STATUSES}")
    if not isinstance(grounded_in_tar_ids, list):
        raise TypeError("grounded_in_tar_ids must be a list")

    return {
        "id": "",  # stamped by writer
        "term_id": term_id,
        "row_type": "query",
        "analysis_lens": analysis_lens,
        "stage": stage,
        "query_index": query_index,
        "query_sql": query_sql,
        "query_result_json": query_result_json,
        "result_row_count": result_row_count,
        "interpretation": interpretation,
        "grounded_in_tar_ids": json.dumps(
            grounded_in_tar_ids, ensure_ascii=False,
        ),
        "sufficiency_json": "",  # query rows don't carry sufficiency
        "status": status,
        "confidence": "",  # query rows don't carry confidence
        "executed_at_utc": "",  # stamped by writer
        "executed_by": "",  # stamped by writer
        "superseded_by": "",
        "run_id": "",  # stamped by writer
        "llm_usage_json": llm_usage_json,
        "validation_errors_json": "",
        "error_message": error_message,
    }


def build_sufficiency_row(
    *,
    term_id: str,
    sufficiency_json: dict,
    confidence: str,
    query_index: int,
    grounded_in_tar_ids: list[str],
    status: str = "success",
    llm_usage_json: str = "{}",
    error_message: str = "",
) -> dict:
    """Build a sufficiency-type TAR row dict. Validates enum fields.

    `sufficiency_json` carries per-lens decisions + reflection summary +
    loop iteration count + declared_sufficient + sufficiency_rationale +
    blockers_resolution. The runner assembles; this helper serializes.

    `error_message` (KI-113) is always empty for sufficiency rows in
    practice (the terminal LLM call doesn't execute SQL), but the
    parameter exists for schema symmetry across both row builders.
    """
    if confidence not in {"high", "medium", "low", ""}:
        raise ValueError(f"confidence={confidence!r} not in enum")
    if status not in _ALLOWED_STATUSES:
        raise ValueError(f"status={status!r} not in {_ALLOWED_STATUSES}")
    if not isinstance(sufficiency_json, dict):
        raise TypeError("sufficiency_json must be a dict")
    if not isinstance(grounded_in_tar_ids, list):
        raise TypeError("grounded_in_tar_ids must be a list")

    return {
        "id": "",  # stamped by writer
        "term_id": term_id,
        "row_type": "sufficiency",
        "analysis_lens": "",  # sufficiency rows have no single lens
        "stage": "terminal",
        "query_index": query_index,
        "query_sql": "",
        "query_result_json": "",
        "result_row_count": 0,
        "interpretation": "",
        "grounded_in_tar_ids": json.dumps(
            grounded_in_tar_ids, ensure_ascii=False,
        ),
        "sufficiency_json": json.dumps(
            sufficiency_json, ensure_ascii=False, default=str,
        ),
        "status": status,
        "confidence": confidence,
        "executed_at_utc": "",  # stamped by writer
        "executed_by": "",  # stamped by writer
        "superseded_by": "",
        "run_id": "",  # stamped by writer
        "llm_usage_json": llm_usage_json,
        "validation_errors_json": "",
        "error_message": error_message,
    }


# ─── CSV I/O ─────────────────────────────────────────────────────────────

def _read_tar_csv() -> list[dict]:
    """Read all rows from the TAR CSV. Returns empty list if missing."""
    if not _TAR_CSV.exists():
        return []
    with _TAR_CSV.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_tar_csv(rows: list[dict]) -> None:
    """Atomic write: `.tmp` + os.replace. LF-only line endings per RULE 34."""
    tmp = _TAR_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=TAR_FIELDS,
            lineterminator="\n", quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in TAR_FIELDS})
    os.replace(tmp, _TAR_CSV)


# ─── Public API ──────────────────────────────────────────────────────────

def peek_next_tar_ids(n_queries: int) -> tuple[str, list[str]]:
    """KI-109: predict the (sufficiency_id, query_ids) the next write_tar_run
    call with `n_queries` query rows would allocate, WITHOUT writing.

    Pure read of term_analysis_results.csv. The runner uses this to anchor
    the sufficiency-emit prompt to the actual allocated id range — closes
    the missing-anchor failure mode that drove BG027/BG029 hallucinations.
    """
    existing = _read_tar_csv()
    sufficiency_id = _next_tar_id(existing)
    placeholder = existing + [{"id": sufficiency_id}]
    query_ids: list[str] = []
    for _ in range(n_queries):
        qid = _next_tar_id(placeholder)
        query_ids.append(qid)
        placeholder.append({"id": qid})
    return sufficiency_id, query_ids


def validate_sufficiency_tar_ids(
    sufficiency_payload: dict,
    allocated_tar_ids: list[str],
) -> tuple[bool, Optional[dict]]:
    """KI-109: pure validator for sufficiency_json tar_id citations.

    Both the runner (pre-write retry) and write_tar_run (defense-in-depth
    final persist) consume this. Returns (True, None) when every cited
    id resolves to an allocated id; (False, errors_dict) otherwise.
    errors_dict shape matches the legacy validation_errors_json format.
    """
    allocated_set = set(allocated_tar_ids)
    cited_ids: set[str] = set()
    lens_breakdown: dict[str, dict] = {}
    lc = sufficiency_payload.get("lens_consideration") or {}
    if isinstance(lc, dict):
        for lens, content in lc.items():
            if not isinstance(content, dict):
                continue
            tids = content.get("tar_ids") or []
            if not isinstance(tids, list):
                continue
            cited = {t for t in tids if isinstance(t, str)}
            if not cited:
                continue
            cited_ids |= cited
            lens_breakdown[lens] = {
                "cited": sorted(cited),
                "unresolved": sorted(cited - allocated_set),
            }
    unresolved = cited_ids - allocated_set
    if not unresolved:
        return True, None
    return False, {
        "error_type": "tar_id_mismatch",
        "cited_ids": sorted(cited_ids),
        "allocated_ids_this_run": sorted(allocated_set),
        "unresolved_ids": sorted(unresolved),
        "lens_breakdown": lens_breakdown,
    }


def validate_query_grounded_in_tar_ids(
    query_rows: list[dict],
    candidate_prior_tar_ids: list[str],
) -> tuple[bool, Optional[dict]]:
    """KI-111: pure validator for per-query grounded_in_tar_ids citations.

    Each query row's `grounded_in_tar_ids` must be a subset of the
    `candidate_prior_tar_ids` surfaced in the bundle (cross-term
    knowledge-reuse pool from load_candidate_prior_tars). Citations
    outside this set are LLM hallucinations and must not be persisted
    as authoritative — runner-side construction is not feasible because
    the LLM is making a discretionary "this prior TAR motivated this
    query" decision the runner cannot reproduce.

    Returns (True, None) when every cited id resolves; (False, errors)
    otherwise. Pure function; caller decides action (strip + log,
    raise, retry).

    errors_dict shape:
      {
        "error_type": "grounded_in_tar_id_mismatch",
        "candidate_prior_tar_ids": sorted list of bundle ids,
        "violations": [
          {"query_index": int, "analysis_lens": str, "stage": str,
           "cited": sorted list, "unresolved": sorted list},
          ...
        ],
      }
    """
    candidate_set = set(candidate_prior_tar_ids or [])
    violations: list[dict] = []
    for qr in query_rows:
        cited = qr.get("grounded_in_tar_ids") or []
        if not cited:
            continue
        cited_set = {t for t in cited if isinstance(t, str)}
        if not cited_set:
            continue
        unresolved = sorted(cited_set - candidate_set)
        if unresolved:
            violations.append({
                "query_index": qr.get("query_index"),
                "analysis_lens": qr.get("analysis_lens"),
                "stage": qr.get("stage"),
                "cited": sorted(cited_set),
                "unresolved": unresolved,
            })
    if not violations:
        return True, None
    return False, {
        "error_type": "grounded_in_tar_id_mismatch",
        "candidate_prior_tar_ids": sorted(candidate_set),
        "violations": violations,
    }


def write_tar_run(
    term_id: str,
    query_rows: list[dict],
    sufficiency_row: dict,
    executed_by: str = "system",
    *,
    run_id: Optional[str] = None,
    sync_parquet: bool = True,
) -> tuple[str, list[str]]:
    """Write a complete Stage C run atomically.

    Supersedes any prior `status='success'` rows for this term_id by
    setting their status='superseded' and superseded_by=<new sufficiency
    id>. New rows are appended with fresh TAR-NNNNN ids and status per
    the input dicts.

    Returns (run_id, [new_row_ids]) where new_row_ids is
    [sufficiency_id, *query_row_ids] in write order.

    The sufficiency row id is the first element of the returned list so
    supersede pointers can be set to it without a second pass.

    Parameters
    ----------
    term_id : str
        FK to business_glossary.id.
    query_rows : list[dict]
        Dicts built via build_query_row. May be empty.
    sufficiency_row : dict
        Dict built via build_sufficiency_row.
    executed_by : str
        User id or 'system'.
    run_id : str, optional
        Override the generated run_id (used by tests). If None,
        generated via build_run_id.
    sync_parquet : bool
        If True (default), trigger parquet re-sync after write. Set
        False in test harnesses that restore CSV afterward.
    """
    if sufficiency_row.get("row_type") != "sufficiency":
        raise ValueError("sufficiency_row['row_type'] must be 'sufficiency'")
    for qr in query_rows:
        if qr.get("row_type") != "query":
            raise ValueError("query_rows entries must have row_type='query'")

    now = _now_utc_naive()
    now_iso = now.isoformat(timespec="seconds")
    final_run_id = run_id or build_run_id(term_id, at=now)

    existing = _read_tar_csv()

    # Generate new ids. Sufficiency row first so supersede pointers can
    # reference it.
    sufficiency_id = _next_tar_id(existing)
    # Pretend sufficiency row has been inserted so subsequent _next_tar_id
    # calls produce fresh ids.
    placeholder_existing = existing + [{"id": sufficiency_id}]
    query_ids: list[str] = []
    for _ in query_rows:
        qid = _next_tar_id(placeholder_existing)
        query_ids.append(qid)
        placeholder_existing.append({"id": qid})

    # Supersede prior rows. Mutation happens on the in-memory list; CSV
    # is rewritten atomically at the end.
    for row in existing:
        if row.get("term_id") == term_id and row.get("status") == "success":
            row["status"] = "superseded"
            row["superseded_by"] = sufficiency_id

    # Stamp new rows with generated ids + timestamps + run_id + user.
    sufficiency_row = dict(sufficiency_row)  # defensive copy

    # KI-102 / KI-109: validate sufficiency tar_id citations against this
    # run's allocations. Defense-in-depth — the runner now retries on
    # validation failure pre-write (KI-109 Layer 1), so reaching this
    # branch means retry exhausted; quarantine to preserve query rows.
    allocated_this_run = [sufficiency_id, *query_ids]
    _suff_str = sufficiency_row.get("sufficiency_json") or "{}"
    try:
        _suff_parsed = json.loads(_suff_str)
    except (json.JSONDecodeError, TypeError):
        _suff_parsed = {}
    is_valid, errors = validate_sufficiency_tar_ids(
        _suff_parsed, allocated_this_run,
    )
    if not is_valid:
        sufficiency_row["status"] = "quarantined"
        sufficiency_row["validation_errors_json"] = json.dumps(
            errors, ensure_ascii=False,
        )
        print(
            f"[WARN] _tar_writer: tar_id citation mismatch in run "
            f"{final_run_id}: {len(errors['unresolved_ids'])} of "
            f"{len(errors['cited_ids'])} cited ids not allocated this "
            f"run; quarantining sufficiency",
            file=sys.stderr,
        )

    sufficiency_row["id"] = sufficiency_id
    sufficiency_row["executed_at_utc"] = now_iso
    sufficiency_row["executed_by"] = executed_by
    sufficiency_row["run_id"] = final_run_id

    stamped_query_rows: list[dict] = []
    for qr, qid in zip(query_rows, query_ids):
        qr2 = dict(qr)
        qr2["id"] = qid
        qr2["executed_at_utc"] = now_iso
        qr2["executed_by"] = executed_by
        qr2["run_id"] = final_run_id
        stamped_query_rows.append(qr2)

    # Merge: existing (with supersede flips) + new rows.
    final_rows = existing + stamped_query_rows + [sufficiency_row]

    _write_tar_csv(final_rows)

    if sync_parquet:
        try:
            from _parquet_sync import sync_parquet_and_invalidate
            sync_parquet_and_invalidate(
                project_root=_ROOT,
                seed_name="term_analysis_results",
                skip=False,
                source="_tar_writer.write_tar_run",
            )
        except Exception as e:  # noqa: BLE001
            # Parquet sync is best-effort; CSV + DuckDB are source of
            # truth. Log but don't fail the write.
            print(
                f"[WARN] _tar_writer: parquet sync failed: "
                f"{type(e).__name__}: {e}",
                file=sys.stderr,
            )

    new_ids = [sufficiency_id] + query_ids
    return final_run_id, new_ids
