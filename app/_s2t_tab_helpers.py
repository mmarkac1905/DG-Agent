"""Stage D.2 — S2T tab helpers for tab_spec in Business_Glossary.py.

Keeps tab_spec body declarative by delegating:
  - (status, term_s2t.empty) action dispatch -> `get_s2t_action`
  - pipeline progress strip rendering -> `render_pipeline_strip`
  - status badge rendering -> `render_status_badge`
  - status-specific details panel rendering -> `render_details_panel`

Import convention: `from _s2t_tab_helpers import ...` — app/ is sys.path
root via Streamlit page discovery, NOT a package.

The sys.path insert at module top mirrors `app/claude_api.py` and is
needed because `check_term_eda_prereq` lives in `scripts/`, which is not
on app/'s import path by default.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd

_TRAILING_DIGITS_RE = re.compile(r"(\d+)$")

# ─── known_issue #84 — dbt-error classifier for deploy auto-retry ──────

_BINDER_COL_RE = re.compile(r'Referenced column "([^"]+)" not found')
_BINDER_CAND_RE = re.compile(r"Candidate bindings?:\s*([^\n]+)")
_FAILED_MODEL_RE = re.compile(r"Failure in model\s+(\w+)\s*\(")

# Error-class hints — injected into repair_dbt_model_sql's user prompt
# before the raw dbt error text so the LLM sees the class-specific
# directive alongside the error body and schema dump.

_HINT_BINDER_COLUMN = (
    "A column referenced in the SQL doesn't exist in its source "
    "table. The dbt error text lists the failed column and candidate "
    "bindings. Replace the bad column name with one from the "
    "candidates or from the information_schema dump. Do not invent "
    "new column names."
)

_HINT_CATALOG = (
    "A table or ref() target doesn't exist. Check every "
    "`{{ ref(...) }}` and schema-qualified reference against the "
    "schema dump; the table may be in a different schema or have a "
    "different name. Use only names present in the schema dump."
)

_HINT_SYNTAX = (
    "The SQL has a syntax problem (unclosed paren, missing comma, "
    "reserved word used as identifier, malformed CTE, etc.). Rewrite "
    "the SQL with correct DuckDB syntax. Keep analytical logic, "
    "JOIN structure, CTEs, and filters identical — only fix syntax."
)

_HINT_TYPE = (
    "A column's type doesn't match the operation — e.g., JOIN on "
    "columns with different types, or a CAST / aggregation failing "
    "on values it can't convert. Check the schema dump for each "
    "column's actual type and add explicit CAST(...) conversions "
    "where needed."
)

_HINT_SPILL = (
    "The query produced too many intermediate rows, causing DuckDB "
    "to spill to disk and fail. This indicates a JOIN on a "
    "LOW-SELECTIVITY key where each row on one side matches MANY "
    "rows on the other — for example, joining on a shared "
    "material / category / classification ID between tables that "
    "represent DIFFERENT entity grains (equipment units, movement "
    "events, serial numbers, etc.). Review EVERY JOIN condition. "
    "Prefer per-entity keys (EQUNR for equipment, SERNR for serials, "
    "MBLNR + MJAHR for material documents) over shared classification "
    "keys (MATNR alone, EQART, category codes). If a table is used "
    "only to filter another, use EXISTS or a semi-join instead of "
    "JOIN + DISTINCT."
)

_HINT_GENERIC = (
    "Review the error text, the current SQL, and the "
    "information_schema dump. Identify what the SQL assumes that "
    "differs from the actual schema, and produce a minimal corrected "
    "SQL."
)


def classify_dbt_error(err_text) -> dict:
    """Classify a dbt run error for the deploy retry loop (known_issue
    #84). Returns a dict with:

      should_retry  bool   — whether to attempt LLM repair.
      hint          str    — class-specific directive for the repair
                             prompt (empty string when should_retry=False).
      failed_col    str|None — Binder column-not-found only.
      candidates    list[str] — Binder candidate bindings.
      failed_model  str|None — parsed from "Failure in model X" when
                             present (useful for targeting the .sql path
                             to repair in multi-model deploys).

    Taxonomy (checked in priority order):
      1. Timeout                         → no retry.
      2. Empty/whitespace                → generic retry.
      3. Binder column-not-found         → narrow Binder hint.
      4. Catalog / table-not-found       → catalog hint.
      5. Parser / syntax                 → syntax hint.
      6. Type mismatch / conversion      → type hint.
      7. IO / OOM / spill (today's case) → cartesian/join-cardinality hint.
      8. Unknown / catch-all             → generic hint.
    """
    text = err_text if isinstance(err_text, str) else ""
    failed_model_m = _FAILED_MODEL_RE.search(text)
    failed_model = failed_model_m.group(1) if failed_model_m else None
    base = {
        "should_retry": True,
        "hint": _HINT_GENERIC,
        "failed_col": None,
        "candidates": [],
        "failed_model": failed_model,
    }

    # 1. Timeouts — LLM can't fix a hung query.
    if "Timed out" in text or "TimeoutExpired" in text:
        return {**base, "should_retry": False, "hint": ""}

    # 2. Empty error — retry with generic hint.
    if not text.strip():
        return base

    # 3. Binder column-not-found (the original retry case, most specific).
    col_m = _BINDER_COL_RE.search(text)
    if col_m:
        cand_m = _BINDER_CAND_RE.search(text)
        cands: list[str] = []
        if cand_m:
            cands = [
                c.strip().strip('"').strip("'")
                for c in cand_m.group(1).split(",")
                if c.strip()
            ]
        return {
            **base,
            "hint": _HINT_BINDER_COLUMN,
            "failed_col": col_m.group(1),
            "candidates": cands,
        }

    # 4. Catalog / table-not-found.
    if ("CatalogException" in text
            or "does not exist" in text
            or "Table with name" in text):
        return {**base, "hint": _HINT_CATALOG}

    # 5. Parser / syntax.
    if "Parser Error" in text or "syntax error" in text.lower():
        return {**base, "hint": _HINT_SYNTAX}

    # 6. Type mismatch / conversion.
    if ("Conversion Error" in text
            or "type mismatch" in text.lower()
            or "Could not convert" in text):
        return {**base, "hint": _HINT_TYPE}

    # 7. IO / OOM / spill — today's BG027 trigger class.
    lowered = text.lower()
    if ("io error" in lowered
            or "failed to create directory" in lowered
            or "out of memory" in lowered
            or "insufficient memory" in lowered):
        return {**base, "hint": _HINT_SPILL}

    # 8. Catch-all — retry with the generic hint already in `base`.
    return base


def extract_trailing_digits(value) -> int:
    """Pull the trailing digits off any string ID; return 0 otherwise.

    Used by Create S2T deploy to find max existing s2t_mapping.id
    across mixed ID schemes. known_issue #83 fix — the legacy
    `x.replace('S', '')` stripped every 'S' character (not just a
    leading prefix) and crashed on modern `S2T-NNNN` IDs.

    Handles all current schemes:
      - SNNN      (legacy):      'S037' → 37
      - S2T-NNNN  (modern):      'S2T-0001' → 1
      - BG028-NN  (term-prefix): 'BG028-07' → 7
      - arbitrary prefix + digits: works regardless of what's before
        the trailing number, as long as the ID ends in digits.

    Returns 0 for IDs that don't end in digits, non-strings, or NaN
    (so `.apply(extract_trailing_digits).max()` stays finite even on
    pathological CSVs).
    """
    if not isinstance(value, str):
        return 0
    m = _TRAILING_DIGITS_RE.search(value)
    return int(m.group(1)) if m else 0

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from _term_eda_prereq import check_term_eda_prereq  # noqa: E402

from db import get_connection  # noqa: E402


# ─── Palettes + stage mapping ──────────────────────────────────────────

# Single source of truth for term-status badge coloring. Replaces the
# two inline dict literals that previously lived at Business_Glossary.py
# lines 2087 and 2132. Keep keys in sync with
# dbt/seeds/schema.yml `business_glossary.status` accepted_values.
STATUS_COLORS = {
    "approved": "#4ade80",
    "draft": "#fbbf24",
    "denied": "#f87171",
    "scope_confirmed": "#60a5fa",
    "term_eda_pending": "#a78bfa",
    "ready_for_s2t": "#34d399",
    "archived": "#9ca3af",
    "unknown": "#6b7280",
}

# Pipeline stage indices. The strip renders six pills:
#   0:Define  1:Scope  2:Domain EDA  3:Term EDA  4:S2T  5:Approve
# scope_confirmed maps to 2 because "Scope" itself is complete once the
# term reaches that status; Domain EDA is the current stage. The strip's
# text stays constant; action-panel text disambiguates "just confirmed"
# vs "Domain EDA in progress" via DAR coverage.
_STATUS_TO_STAGE_IDX = {
    "draft": 0,
    "scope_confirmed": 2,
    "term_eda_pending": 3,
    "ready_for_s2t": 4,
    "approved": 5,
    "denied": 5,
    "archived": -1,
}

_PIPELINE_STAGES = ("Define", "Scope", "Domain EDA", "Term EDA", "S2T", "Approve")

_PER_TABLE_ANALYZERS = (
    "completeness", "dimensions", "magnitude", "code_tables",
    "date", "segmentation",
)


def status_to_stage_index(status: str) -> int:
    return _STATUS_TO_STAGE_IDX.get(status, -1)


def has_piece8_s2t_rows(term_s2t_df) -> bool:
    """Discriminator for "Create-S2T + Deploy has completed."

    Stage A's `_rewrite_s2t_for_term` writes s2t_mapping rows at
    scope_confirmed time with empty `target_model` — those rows exist
    solely to carry scope table + source field + rationale, not a
    target mapping. The Deploy handler writes rows with
    `target_model` populated (e.g. 'fact_cpe_deployments').

    Returns True iff at least one row in the slice has a non-empty
    target_model. For a term still at Stage A (pre-Create-S2T), this
    returns False even though the DataFrame isn't strictly empty —
    fixing the gating regression where term_s2t.empty routed BG027 and
    other pipeline terms into the existing-S2T lineage branch
    before they had any Deploy output.
    """
    if term_s2t_df is None or term_s2t_df.empty:
        return False
    if 'target_model' not in term_s2t_df.columns:
        return False
    # `.astype(str)` alone leaves NaN as NaN (pandas quirk), so `.fillna('')`
    # first to normalize None/NaN/'' into a single empty-string check.
    target_models = term_s2t_df['target_model'].fillna('').astype(str).str.strip()
    populated = (
        (target_models != '') &
        (target_models != 'None') &
        (target_models != 'nan')
    )
    # Coerce numpy.bool_ → Python bool so `is False`/`is True` identity
    # comparisons in callers (including test harnesses) behave predictably.
    return bool(populated.any())


def is_s2t_eligible(term, has_legacy_findings: bool) -> tuple[bool, str]:
    """Stage D.2 dual-path gate. Pure function — no Streamlit calls.

    Returns `(eligible, reason)`. Reason is for audit/testing; user-facing
    messaging lives in tab_spec's action panel.

    - 'archived' is a hard stop regardless of findings.
    - New-pipeline terms (status in ready_for_s2t / approved) are eligible
      even without legacy analysis_findings.
    - Legacy-path terms (any status, has analysis_findings) remain eligible.

    `term` is a pandas Series or dict-like with a `status` field.
    """
    status = str((term.get("status") if hasattr(term, "get") else term["status"]) or "").strip().lower()
    if status == "archived":
        return (False, "archived_hard_stop")
    is_new_pipeline = status in ("ready_for_s2t", "approved")
    if has_legacy_findings or is_new_pipeline:
        return (True, "eligible")
    return (False, f"ineligible_status_{status}")


def _safe_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    return str(v)


# ─── Data fetchers ─────────────────────────────────────────────────────

def _has_analysis_findings(term_id: str) -> bool:
    """Legacy-path eligibility probe. True iff analysis_findings has any
    row for this term."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT 1 FROM main_seeds.analysis_findings "
        "WHERE business_term_id = ? LIMIT 1",
        [term_id],
    ).fetchall()
    return len(rows) > 0


def _get_scope_tables(term_id: str) -> list[str]:
    """Scope tables for the term.

    Primary source: s2t_mapping (post-Deploy).
    Fallback: business_glossary.scope_derivation_history_json latest
    confirmed iteration (pre-Deploy, Stage A output).
    Returns [] if neither yields rows."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT LOWER(source_table) FROM main_seeds.s2t_mapping "
        "WHERE business_term_id = ? ORDER BY 1",
        [term_id],
    ).fetchall()
    if rows:
        return [r[0] for r in rows if r[0]]

    history_rows = conn.execute(
        "SELECT scope_derivation_history_json FROM main_seeds.business_glossary "
        "WHERE id = ?",
        [term_id],
    ).fetchall()
    if not history_rows or not history_rows[0][0]:
        return []
    try:
        history = json.loads(history_rows[0][0])
    except (json.JSONDecodeError, TypeError):
        return []
    iterations = history.get("iterations", []) or []
    if not iterations:
        return []
    confirmed = next(
        (it for it in iterations if it.get("analyst_action") == "confirmed"),
        iterations[-1],
    )
    tables = (confirmed.get("llm_response") or {}).get("proposed_tables") or []
    return sorted({str(t) for t in tables if t})


def _get_latest_tar_sufficiency(term_id: str) -> dict | None:
    """Latest successful sufficiency-type TAR row, unpacked into a dict.
    Returns None if no such row exists or sufficiency_json is unparseable."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT sufficiency_json, confidence, run_id, executed_at_utc
        FROM main_seeds.term_analysis_results
        WHERE term_id = ? AND row_type = 'sufficiency' AND status = 'success'
        ORDER BY executed_at_utc DESC LIMIT 1
        """,
        [term_id],
    ).fetchall()
    if not rows:
        return None
    raw_json, confidence, run_id, executed_at = rows[0]
    try:
        sj = json.loads(raw_json) if raw_json else {}
    except (json.JSONDecodeError, TypeError):
        sj = {}
    return {
        "confidence": confidence or "",
        "declared_sufficient": sj.get("declared_sufficient"),
        "lens_consideration": sj.get("lens_consideration", {}) or {},
        "blockers_resolution": sj.get("blockers_resolution", []) or [],
        "run_id": run_id or "",
        "executed_at_utc": str(executed_at or ""),
    }


def _get_prereq(term_id: str) -> dict:
    """Delegate to Stage D.1's canonical prereq check. Do NOT re-derive
    coverage logic."""
    return check_term_eda_prereq(get_connection(), term_id)


# ─── Action dispatcher ─────────────────────────────────────────────────

def _base_action() -> dict:
    return {
        "action_text": "",
        "details_key": None,
        "details_data": {},
        "deep_link_target": None,
        "deep_link_label": None,
        "deep_link_hint": None,
        "show_create_button": False,
        "note": None,
    }


def get_s2t_action(
    status: str,
    term_id: str,
    has_piece8_mapping: bool,
    glossary_row: dict,
) -> dict:
    """Return action descriptor for tab_spec based on (status, has_piece8_mapping).

    `has_piece8_mapping` is True iff Create-S2T + Deploy has
    produced at least one s2t_mapping row with populated `target_model`
    for this term. Use `has_piece8_s2t_rows(term_s2t)` at the call site.
    This replaces the earlier `term_s2t_empty` flag which was False
    for Stage A-only rows (target_model=NULL), misrouting
    pipeline terms into the existing-S2T branch.

    `glossary_row` is a dict-like (pandas Series .to_dict() or dict)
    carrying the term's business_glossary row fields — notes,
    archive_id, archived_at_utc, archived_reason_code/text.

    Rendering keys (all present; None/empty when not used):
      action_text: str
      details_key: str | None
      details_data: dict
      deep_link_target: str | None   (cross-page st.page_link)
      deep_link_label: str | None    (pairs with deep_link_target)
      deep_link_hint: str | None     (same-page caption)
      show_create_button: bool
      note: str | None
    """
    a = _base_action()

    if status == "draft":
        a["action_text"] = "This term needs a definition before the pipeline can start."
        a["deep_link_hint"] = "Switch to Term Detail tab above to edit"
        return a

    if status == "scope_confirmed":
        prereq = _get_prereq(term_id)
        scope_tables = list(prereq.get("scope_tables") or [])
        missing_map = dict(prereq.get("missing_analyzers_per_table") or {})
        missing_pairs = list(prereq.get("missing_grain_pairs") or [])
        total = len(scope_tables)
        covered = max(0, total - len(missing_map))
        any_missing = bool(missing_map) or bool(missing_pairs)

        if total > 0 and covered == 0 and any_missing:
            a["action_text"] = "Scope confirmed. Next: run Domain EDA on scope tables."
            a["deep_link_target"] = "pages/Data_Analysis.py"
            a["deep_link_label"] = "Go to Domain Analysis"
        elif any_missing:
            a["action_text"] = (
                f"Domain EDA in progress. {covered} of {total} scope tables fully covered."
            )
            a["deep_link_target"] = "pages/Data_Analysis.py"
            a["deep_link_label"] = "Go to Domain Analysis"
        else:
            a["action_text"] = "Domain EDA complete. Ready for Term EDA."
            a["deep_link_target"] = "pages/Data_Analysis.py"
            a["deep_link_label"] = "Go to Business Term Analysis"

        a["details_key"] = "scope_coverage"
        a["details_data"] = {
            "scope_tables": scope_tables,
            "prereq_response": prereq,
        }
        return a

    if status == "term_eda_pending":
        prereq = _get_prereq(term_id)
        a["action_text"] = (
            "Term EDA in progress or has pending analyst acknowledgements."
        )
        a["details_key"] = "tar_summary"
        a["details_data"] = {
            "scope_tables": list(prereq.get("scope_tables") or []),
            "prereq_response": prereq,
            "tar_sufficiency": _get_latest_tar_sufficiency(term_id),
        }
        a["deep_link_target"] = "pages/Data_Analysis.py"
        a["deep_link_label"] = "Go to Business Term Analysis"
        return a

    if status == "ready_for_s2t":
        if not has_piece8_mapping:
            prereq = _get_prereq(term_id)
            a["action_text"] = "Ready for S2T. Review grounding, then create."
            a["details_key"] = "tar_summary"
            a["details_data"] = {
                "scope_tables": list(prereq.get("scope_tables") or []),
                "prereq_response": prereq,
                "tar_sufficiency": _get_latest_tar_sufficiency(term_id),
            }
            a["show_create_button"] = True
        else:
            a["action_text"] = "S2T created. Awaiting analyst approval."
            a["details_key"] = "awaiting_approval"
            a["deep_link_hint"] = "Switch to Term Detail tab above for approval"
        return a

    if status == "approved":
        if not has_piece8_mapping:
            prereq = _get_prereq(term_id)
            a["action_text"] = (
                "Term approved but S2T not yet created. Proceed to create."
            )
            a["details_key"] = "tar_summary"
            a["details_data"] = {
                "scope_tables": list(prereq.get("scope_tables") or []),
                "prereq_response": prereq,
                "tar_sufficiency": _get_latest_tar_sufficiency(term_id),
            }
            a["show_create_button"] = True
        else:
            a["action_text"] = "S2T approved and deployed."
            a["note"] = (
                "To re-run S2T, deny approval first (preserves audit trail) — "
                "Re-run path deferred to follow-up commit."
            )
        return a

    if status == "denied":
        notes = _safe_str(glossary_row.get("notes", ""))
        excerpt = notes[-500:] if notes else None
        a["action_text"] = "S2T was denied by analyst. Review denial reason."
        a["details_key"] = "denial_info"
        a["details_data"] = {"notes_excerpt": excerpt}
        a["deep_link_hint"] = "Switch to Term Detail tab above for re-approval"
        return a

    if status == "archived":
        at_utc = _safe_str(glossary_row.get("archived_at_utc"))
        reason_code = _safe_str(glossary_row.get("archived_reason_code"))
        a["action_text"] = (
            f"This term is archived on {at_utc or '(unknown date)'} — "
            f"reason: {reason_code or '(not recorded)'}."
        )
        a["details_key"] = "archive_info"
        a["details_data"] = {
            "archive_id": _safe_str(glossary_row.get("archive_id")),
            "archived_at_utc": at_utc,
            "archived_reason_code": reason_code,
            "archived_reason_text": _safe_str(glossary_row.get("archived_reason_text")),
        }
        return a

    # Unknown / unhandled — safe fallback keeps the page rendering.
    a["action_text"] = f"Term is in status '{status}' — no defined action."
    return a


# ─── Renderers ─────────────────────────────────────────────────────────

def render_status_badge(status: str, st) -> None:
    color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
    label = (status or "unknown").upper()
    st.markdown(
        f"**Status:** <span style='color:{color};font-weight:bold;font-size:16px'>"
        f"{label}</span>",
        unsafe_allow_html=True,
    )


def render_pipeline_strip(status: str, st) -> None:
    current_idx = status_to_stage_index(status)
    is_denied = (status == "denied")
    cols = st.columns(6)
    for i, col in enumerate(cols):
        stage = _PIPELINE_STAGES[i]
        if status == "archived":
            col.markdown(
                f"<div style='opacity:0.4;text-align:center'>{stage}</div>",
                unsafe_allow_html=True,
            )
            continue
        if i < current_idx:
            color = STATUS_COLORS["ready_for_s2t"]
            col.markdown(
                f"<div style='color:{color};text-align:center'>✓ {stage}</div>",
                unsafe_allow_html=True,
            )
        elif i == current_idx:
            if is_denied:
                color = STATUS_COLORS["denied"]
                col.markdown(
                    f"<div style='color:{color};font-weight:bold;text-align:center'>"
                    f"✗ {stage}</div>",
                    unsafe_allow_html=True,
                )
            else:
                color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
                col.markdown(
                    f"<div style='color:{color};font-weight:bold;text-align:center'>"
                    f"{stage}</div>",
                    unsafe_allow_html=True,
                )
        else:
            col.markdown(
                f"<div style='opacity:0.5;text-align:center'>{stage}</div>",
                unsafe_allow_html=True,
            )
    if status == "archived":
        st.caption("🗃️ **Archived** — pipeline halted.")


def render_details_panel(details_key, details_data: dict, st) -> None:
    if not details_key:
        return
    data = details_data or {}
    if details_key == "scope_coverage":
        _render_scope_coverage(data, st)
    elif details_key == "tar_summary":
        _render_tar_summary(data, st)
    elif details_key == "awaiting_approval":
        _render_awaiting_approval(data, st)
    elif details_key == "archive_info":
        _render_archive_info(data, st)
    elif details_key == "denial_info":
        _render_denial_info(data, st)
    else:
        st.warning(f"Unknown details_key: {details_key}")


def _render_scope_coverage(data: dict, st) -> None:
    scope_tables = list(data.get("scope_tables") or [])
    prereq = dict(data.get("prereq_response") or {})
    missing_map = dict(prereq.get("missing_analyzers_per_table") or {})
    missing_pairs = list(prereq.get("missing_grain_pairs") or [])

    if not scope_tables:
        st.caption("_No scope tables defined yet._")
        return

    st.caption("**Scope tables + per-analyzer coverage:**")
    header = ["Table"] + list(_PER_TABLE_ANALYZERS)
    rows = []
    for t in scope_tables:
        missing = set(missing_map.get(t) or [])
        row = [t] + ["✗" if a in missing else "✓" for a in _PER_TABLE_ANALYZERS]
        rows.append(row)
    df = pd.DataFrame(rows, columns=header)
    st.dataframe(df, use_container_width=True, hide_index=True)

    if missing_pairs:
        pairs_str = ", ".join(f"({a}, {b})" for a, b in missing_pairs)
        st.caption(f"**Missing grain_relationship pairs:** {pairs_str}")


def _render_tar_summary(data: dict, st) -> None:
    _render_scope_coverage(data, st)
    tar = data.get("tar_sufficiency")
    if tar is None:
        st.caption("_No Term EDA run yet._")
        return
    conf = tar.get("confidence") or ""
    ds = tar.get("declared_sufficient")
    st.caption(
        f"**Term EDA sufficiency:** confidence=`{conf.upper() or '?'}` · "
        f"declared_sufficient=`{ds}` · run_id=`{tar.get('run_id', '')}`"
    )
    lens = tar.get("lens_consideration") or {}
    if lens:
        considered = sum(
            1 for v in lens.values()
            if isinstance(v, dict) and (v.get("decision") or "") != "skipped"
        )
        st.caption(f"**Lenses considered:** {considered} of {len(lens)}")
    blockers = tar.get("blockers_resolution") or []
    if blockers:
        counts: dict[str, int] = {}
        for b in blockers:
            s = b.get("status") if isinstance(b, dict) else None
            if s:
                counts[s] = counts.get(s, 0) + 1
        if counts:
            counts_str = " · ".join(f"{k}: {v}" for k, v in counts.items())
            st.caption(f"**Blocker resolutions:** {counts_str}")


def _render_awaiting_approval(data: dict, st) -> None:
    st.info(
        "S2T mapping has been generated and deployed. "
        "The term is awaiting analyst approval via the Term Detail tab."
    )


def _render_archive_info(data: dict, st) -> None:
    at_utc = data.get("archived_at_utc") or "(unknown date)"
    reason_code = data.get("archived_reason_code") or "(not recorded)"
    st.warning(f"**Archived** on {at_utc} — reason: {reason_code}")
    reason_text = data.get("archived_reason_text") or ""
    if reason_text:
        st.caption(reason_text)
    arch_id = data.get("archive_id") or ""
    if arch_id:
        st.caption(
            f"Archive ID: `{arch_id}` — see archive_log for full audit trail."
        )


def _render_denial_info(data: dict, st) -> None:
    st.warning("This term's S2T was denied by the analyst.")
    excerpt = data.get("notes_excerpt")
    if excerpt:
        st.caption("**Term notes (context — may include denial reason):**")
        st.text(excerpt)
    else:
        st.caption("See term notes in Term Detail tab for context.")
