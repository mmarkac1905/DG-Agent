"""Piece 9 Stage B + C4 — Stage A blocker loaders.

Reads Stage A's scope_derivation_history_json from business_glossary.csv
(CSV-first, matching _scope_derivation.py::load_scope_history).

Two consumption surfaces:

  load_blockers_for_table (Piece 9 Stage B)
    - Per-table: filters by resolves_in='domain_eda' AND blocker.tables
      contains target. Used by the 4 Domain EDA analyzers
      (run_completeness, run_dimensions, run_magnitude, run_code_tables).

  load_blockers_for_term (C4 — Theme 1 sub-item 5)
    - Per-term: returns ALL confirmed-iteration blockers regardless of
      resolves_in. Used by the iteration prompt context assembler so the
      iteration LLM can see Stage A's known-concerns directly (rather
      than only via the lossy DAR/TAR resolution round-trip).

Design docs:
  - context/phase_15b_piece_8_pre_s2t_reasoning_layer.md §28.11.2 (Stage B)
  - tasks/c4_step0_report.md (C4)

Contract highlights:
  - Source of truth: dbt/seeds/business_glossary.csv (not DuckDB); in-flight
    confirmations may not have re-seeded yet.
  - Eligible rows: status IN ('scope_confirmed', 'domain_eda_pending',
    'term_eda_pending', 'ready_for_s2t').
  - Confirmed iteration resolution: analyst_action == 'confirmed' wins;
    final_iter_num is the fallback for pre-augmentation history shapes.
  - Tolerant to malformed JSON, missing resolves_in, and out-of-range
    final_iter_num (never raises).
  - No merging across terms — per-term attribution is preserved.
  - Truncation cap: when more blockers match than max_blockers, sort by
    confirmed_at_utc DESC, keep top N, return the truncated count so the
    caller can render a visible note.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Optional

import duckdb

_ROOT = Path(__file__).resolve().parent.parent
_BG_CSV = _ROOT / "dbt" / "seeds" / "business_glossary.csv"
_DB = _ROOT / "cpe_analytics.duckdb"

_ELIGIBLE_STATUSES = frozenset({
    "scope_confirmed", "domain_eda_pending",
    "term_eda_pending", "ready_for_s2t",
})


# KI-114 — names the view that reconciles Stage A filings with Stage C
# resolutions. When `filter_resolved=True`, load_blockers_for_table joins
# against this view and excludes (term_id, short_title) pairs whose
# current_status='RESOLVED'. Defensive against view absence (returns
# empty resolved set, equivalent to the legacy unfiltered behavior).
_BLOCKER_STATE_VIEW = "main_knowledge.knowledge_blocker_state"


def _load_resolved_blocker_keys(
    conn: Optional[duckdb.DuckDBPyConnection],
) -> set[tuple[str, str]]:
    """Query the unified blocker-state view for (term_id, short_title)
    tuples whose current_status='RESOLVED'. Used by load_blockers_for_table
    when filter_resolved=True (KI-114).

    Returns empty set on:
      - view not yet materialized (initial migration window)
      - DuckDB error opening _DB
      - any query exception (defense-in-depth)

    The empty-set fallback means filter_resolved=True degrades gracefully
    to legacy unfiltered behavior when the view is unavailable.
    """
    own_conn = False
    if conn is None:
        try:
            conn = duckdb.connect(str(_DB), read_only=True)
            own_conn = True
        except Exception:  # noqa: BLE001
            return set()
    try:
        rows = conn.execute(
            f"""
            SELECT term_id, blocker_short_title
            FROM {_BLOCKER_STATE_VIEW}
            WHERE current_status = 'RESOLVED'
            """
        ).fetchall()
    except Exception:  # noqa: BLE001
        return set()
    finally:
        if own_conn:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return {(str(t), str(s)) for t, s in rows if t is not None and s is not None}


def _resolve_confirmed_iteration(history: dict) -> Optional[dict]:
    """Pick the confirmed iteration from a scope_derivation_history_json dict.

    Priority:
      1. iterations[*] with analyst_action == 'confirmed' (current contract).
      2. history.final_iter_num -> iterations[final_iter_num - 1] (fallback for
         pre-augmentation shapes or malformed analyst_action values).

    Returns None when no confirmed iteration can be identified. Tolerant to
    IndexError / TypeError / KeyError on the fallback lookup.
    """
    iterations = history.get("iterations") or []
    if not isinstance(iterations, list):
        return None
    for it in iterations:
        if isinstance(it, dict) and it.get("analyst_action") == "confirmed":
            return it
    # Fallback — final_iter_num is 1-indexed.
    try:
        final_idx = history["final_iter_num"]
        if final_idx is None:
            return None
        candidate = iterations[int(final_idx) - 1]
        if isinstance(candidate, dict):
            return candidate
        return None
    except (IndexError, TypeError, KeyError, ValueError):
        return None


def _iter_eligible_rows(bg_csv: Path):
    """Yield (id, term_name, status, history_dict, confirmed_at_utc) tuples
    for every business_glossary row eligible for Stage B consideration.

    Tolerates malformed JSON per row (skips the row rather than raising).
    """
    if not bg_csv.exists():
        return
    with bg_csv.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            status = (row.get("status") or "").strip()
            if status not in _ELIGIBLE_STATUSES:
                continue
            raw = row.get("scope_derivation_history_json") or "{}"
            try:
                history = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(history, dict):
                continue
            confirmed_at = history.get("confirmed_at_utc") or ""
            yield (
                row.get("id", ""),
                row.get("term_name", ""),
                status,
                history,
                str(confirmed_at),
            )


def load_blockers_for_table(
    table: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    max_blockers: int = 10,
    *,
    filter_resolved: bool = False,
) -> tuple[list[dict], int]:
    """Return Stage A blockers targeting `table` with resolves_in='domain_eda'.

    Scans business_glossary.csv (CSV-first) for rows where status IN
    ('scope_confirmed', 'domain_eda_pending', 'term_eda_pending',
    'ready_for_s2t'). For each such row, locates the confirmed iteration via
    iterations[*].analyst_action == 'confirmed'; falls back to
    history.final_iter_num resolving to iterations[final_iter_num-1]
    (1-indexed). If final_iter_num is out of range (past end, negative, or
    missing when expected), treats the term as unresolvable and skips it.
    Never raises IndexError.

    Filters the confirmed iteration's llm_response.blockers to entries where
    resolves_in == 'domain_eda' AND table.lower() is in the lowercased
    blocker.tables list.

    Returns (blocker_entries, truncation_count):
      blocker_entries: list of dicts shaped:
        {
          "term_id": str,
          "term_name": str,
          "blocker": {...}   # the full blocker dict from Stage A
        }
      truncation_count: 0 when len(all_matching) <= max_blockers; otherwise
      (len(all_matching) - max_blockers). When truncating, sorts by the
      confirming term's confirmed_at_utc DESC and keeps the first
      max_blockers entries.

    Tolerant: malformed JSON -> skip row. Missing 'resolves_in' -> skip
    blocker (natural BG027 exclusion). Confirmed iteration unresolvable ->
    skip term.

    `filter_resolved` (KI-114): when True, additionally excludes blockers
    whose (term_id, short_title) appears in the unified blocker_state view
    with current_status='RESOLVED' (Stage C marked them
    resolved/not_applicable in the latest sufficiency). Production caller
    on the Domain Analysis MSEG/per-table panel passes True; default False
    preserves the legacy unfiltered behavior for tests and other callers
    that genuinely need the unfiltered set (e.g. the iteration prompt's
    blockers section). Defensive against view absence — falls back to
    legacy behavior if the view doesn't exist or the query errors.

    The `conn` parameter is accepted for API symmetry with other scripts;
    used only for the resolved-keys query when filter_resolved=True.
    """
    target = (table or "").lower()
    if not target:
        return ([], 0)

    resolved_keys: set[tuple[str, str]] = (
        _load_resolved_blocker_keys(conn) if filter_resolved else set()
    )

    matches: list[tuple[str, dict]] = []  # (confirmed_at_utc, entry)
    for term_id, term_name, _status, history, confirmed_at in _iter_eligible_rows(_BG_CSV):
        iteration = _resolve_confirmed_iteration(history)
        if iteration is None:
            continue
        resp = iteration.get("llm_response") or {}
        if not isinstance(resp, dict):
            continue
        blockers = resp.get("blockers") or []
        if not isinstance(blockers, list):
            continue
        for b in blockers:
            if not isinstance(b, dict):
                continue
            resolves_in = b.get("resolves_in")
            if resolves_in != "domain_eda":
                continue
            tables = b.get("tables") or []
            if not isinstance(tables, list):
                continue
            if target not in {str(t).lower() for t in tables if t is not None}:
                continue
            short_title = b.get("short_title") or ""
            if filter_resolved and (term_id, short_title) in resolved_keys:
                continue
            entry = {
                "term_id": term_id,
                "term_name": term_name,
                "blocker": b,
            }
            matches.append((confirmed_at, entry))

    if len(matches) <= max_blockers:
        return ([e for _ts, e in matches], 0)

    # Truncate: most recently confirmed first.
    matches.sort(key=lambda x: x[0], reverse=True)
    kept = [e for _ts, e in matches[:max_blockers]]
    truncation_count = len(matches) - max_blockers
    return (kept, truncation_count)


def render_analyst_concerns_block(
    blocker_entries: list[dict],
    truncation_count: int = 0,
) -> str:
    """Render the injected system-prompt sub-section.

    Empty list -> empty string (no heading, no noise).
    Non-empty  -> Markdown listing the analyst concerns associated with
    this table, surfacing them as context for the LLM:

        ### Analyst concerns to address in this analysis

        This table is in scope for {N} downstream business term(s). The
        concerns below describe what those terms need this analysis to
        surface.

        **Concern {idx} — from {term_id} ({term_name})**
        - Short title: {short_title}
        - Blocker type: {type}
        - Target tables: {tables_joined}
        - What it means: {what_it_means}
        - What the analysis needs: {what_llm_needs}
        - Resolves in stage: {resolves_in}
        - Resolution mechanism: {resolves_via}
        - Analyst action now: {user_action_now}

        _Note: {truncation_count} additional blocker(s) truncated for
        prompt size. Showing the {shown} most recently confirmed._

    N is the count of DISTINCT term_ids in blocker_entries (a single term
    contributes once to N even if it raised multiple blockers).

    KI-115 closure note: a prior version of this rendered text instructed
    analyzers to populate a `blockers_addressed` field in their JSON
    output. Empirically analyzers disregarded the directive and the
    downstream `knowledge_blocker_state` view does not consume that
    field — Stage A blockers are reconciled with Stage C
    `blockers_resolution` instead. Directive removed; concern context
    retained for analyst awareness when reviewing LLM output. The
    `blockers_addressed` field still exists in DAR `result_json` schema
    for backward compatibility but is intentionally always-empty.
    """
    if not blocker_entries:
        return ""

    distinct_terms = {e.get("term_id", "") for e in blocker_entries}
    n_distinct = len(distinct_terms)

    lines: list[str] = []
    lines.append("### Analyst concerns to address in this analysis")
    lines.append("")
    lines.append(
        f"This table is in scope for {n_distinct} downstream business "
        f"term(s). The concerns below describe what those terms need "
        f"this analysis to surface."
    )
    lines.append("")

    for idx, entry in enumerate(blocker_entries, start=1):
        b = entry.get("blocker") or {}
        term_id = entry.get("term_id", "")
        term_name = entry.get("term_name", "")
        tables = b.get("tables") or []
        tables_joined = ", ".join(str(t) for t in tables)
        lines.append(f"**Concern {idx} — from {term_id} ({term_name})**")
        lines.append(f"- Short title: {b.get('short_title', '')}")
        lines.append(f"- Blocker type: {b.get('type', '')}")
        lines.append(f"- Target tables: {tables_joined}")
        lines.append(f"- What it means: {b.get('what_it_means', '')}")
        lines.append(f"- What the analysis needs: {b.get('what_llm_needs', '')}")
        lines.append(f"- Resolves in stage: {b.get('resolves_in', '')}")
        lines.append(f"- Resolution mechanism: {b.get('resolves_via', '')}")
        lines.append(f"- Analyst action now: {b.get('user_action_now', '')}")
        lines.append("")

    if truncation_count > 0:
        lines.append(
            f"_Note: {truncation_count} additional blocker(s) truncated "
            f"for prompt size. Showing the {len(blocker_entries)} most "
            f"recently confirmed._"
        )

    return "\n".join(lines)


# ─── C4 — per-term loader for the iteration prompt ────────────────────

def _read_term_history(bg_csv: Path, term_id: str) -> tuple[Optional[dict], str]:
    """Locate `term_id` in business_glossary.csv. Returns
    (history_dict | None, confirmed_at_utc).

    None on: missing CSV, missing row, malformed JSON, ineligible status.
    Eligible status enforcement matches load_blockers_for_table.
    """
    if not bg_csv.exists():
        return (None, "")
    target = (term_id or "").strip()
    if not target:
        return (None, "")
    with bg_csv.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("id") or "").strip() != target:
                continue
            status = (row.get("status") or "").strip()
            if status not in _ELIGIBLE_STATUSES:
                return (None, "")
            raw = row.get("scope_derivation_history_json") or "{}"
            try:
                history = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return (None, "")
            if not isinstance(history, dict):
                return (None, "")
            return (history, str(history.get("confirmed_at_utc") or ""))
    return (None, "")


def load_blockers_for_term(
    term_id: str,
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    max_blockers: int = 10,
) -> tuple[list[dict], int]:
    """C4 — return ALL confirmed-iteration blockers for `term_id`.

    Sibling to load_blockers_for_table; per-term scope, no resolves_in
    filter (per OQ-C4-2: surface every routing value so the iteration
    LLM can route reasoning per-blocker rather than relying on upstream
    filtering).

    Returns (entries, truncation_count):
      entries: list of dicts shaped:
        {
          "iter_num": int,        # 1-indexed iteration number
          "blocker_index": int,   # 0-indexed position within iteration's
                                  # llm_response.blockers list
          "blocker": {...}        # raw blocker dict from Stage A
        }
      truncation_count: 0 when len(blockers) <= max_blockers; otherwise
      (len - max_blockers). Sort key for truncation is the confirming
      term's confirmed_at_utc DESC (stable when single-term, but kept
      for symmetry with load_blockers_for_table).

    Returns ([], 0) on:
      - empty/missing term_id
      - business_glossary.csv missing or term row missing
      - row's status not in _ELIGIBLE_STATUSES
      - malformed scope_derivation_history_json
      - no confirmed iteration resolvable

    Pre-augmentation blockers (per Step 0 §SS-4) — those missing the
    `resolves_in` field — are KEPT (unlike load_blockers_for_table which
    filters them out). The render function annotates them; the iteration
    LLM may rely on these for older terms.

    The `conn` parameter is accepted for API symmetry but is not used —
    CSV is the source of truth (matching load_blockers_for_table).
    """
    _ = conn  # API symmetry only
    history, confirmed_at = _read_term_history(_BG_CSV, term_id)
    if history is None:
        return ([], 0)
    iteration = _resolve_confirmed_iteration(history)
    if iteration is None:
        return ([], 0)

    iter_num_raw = iteration.get("iter_num")
    try:
        iter_num = int(iter_num_raw) if iter_num_raw is not None else 0
    except (TypeError, ValueError):
        iter_num = 0

    resp = iteration.get("llm_response") or {}
    if not isinstance(resp, dict):
        return ([], 0)
    blockers = resp.get("blockers") or []
    if not isinstance(blockers, list):
        return ([], 0)

    entries: list[dict] = []
    for idx, b in enumerate(blockers):
        if not isinstance(b, dict):
            continue
        entries.append({
            "iter_num": iter_num,
            "blocker_index": idx,
            "blocker": b,
        })

    if len(entries) <= max_blockers:
        return (entries, 0)
    # Single-term ordering: keep the first max_blockers (Stage A's own
    # ordering, which mirrors the LLM's emission order). confirmed_at_utc
    # is single-valued for a per-term call, so a sort wouldn't reorder.
    _ = confirmed_at  # kept for parity with the per-table truncation key
    truncation_count = len(entries) - max_blockers
    return (entries[:max_blockers], truncation_count)


def _truncate(value: object, cap: int) -> str:
    """Coerce to string, strip, hard-cap at `cap` chars."""
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) <= cap:
        return s
    return s[: cap - 1].rstrip() + "…"


def render_stage_a_blockers_section(
    entries: list[dict],
    truncation_count: int = 0,
) -> str:
    """C4 — render the iteration-bundle "## Stage A blockers" section.

    Empty entries -> empty string (no heading, no noise).
    Non-empty -> Markdown section per OQ-C4-3:

        ## Stage A blockers

        **Blocker iter1.b0** (term_eda, scope_concern)
        - Tables: mseg
        - Short title: BWART movement-type semantics unclear
        - What it means: <truncate to 280 chars>
        - What the analysis needs: <truncate to 200 chars>
        - Resolves via: <truncate to 200 chars>
        - User action now: <truncate to 200 chars>

        ...

        _Note: N additional blocker(s) truncated for prompt size._

    Pre-augmentation blockers (missing resolves_in) render with
    "(unset)" / "(pre-augmentation)" annotation rather than being
    skipped (per Step 0 §SS-4).

    Per-field truncation caps: 280 chars for what_it_means
    (most context-dense), 200 chars for the other free-form fields.
    """
    if not entries:
        return ""

    lines: list[str] = ["## Stage A blockers", ""]
    for entry in entries:
        iter_num = entry.get("iter_num", 0)
        idx = entry.get("blocker_index", 0)
        b = entry.get("blocker") or {}
        if not isinstance(b, dict):
            continue
        resolves_in = b.get("resolves_in")
        resolves_in_disp = (
            str(resolves_in) if resolves_in
            else "unset (pre-augmentation)"
        )
        btype = str(b.get("type") or "")
        tables = b.get("tables") or []
        if isinstance(tables, list):
            tables_disp = ", ".join(str(t) for t in tables)
        else:
            tables_disp = str(tables)
        lines.append(
            f"**Blocker iter{iter_num}.b{idx}** "
            f"({resolves_in_disp}, {btype})"
        )
        lines.append(f"- Tables: {tables_disp}")
        lines.append(f"- Short title: {_truncate(b.get('short_title'), 200)}")
        lines.append(
            f"- What it means: {_truncate(b.get('what_it_means'), 280)}"
        )
        lines.append(
            f"- What the analysis needs: "
            f"{_truncate(b.get('what_llm_needs'), 200)}"
        )
        lines.append(
            f"- Resolves via: {_truncate(b.get('resolves_via'), 200)}"
        )
        lines.append(
            f"- User action now: {_truncate(b.get('user_action_now'), 200)}"
        )
        lines.append("")

    if truncation_count > 0:
        lines.append(
            f"_Note: {truncation_count} additional blocker(s) truncated "
            f"for prompt size._"
        )
        lines.append("")

    return "\n".join(lines)
