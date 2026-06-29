"""KI #71 Step 3 — guided-unwind archive UI.

Renders the Archive section on a business term's detail page. Three
possible states:

1. **already_archived** — audit panel with archive_id + date. No actions.
2. **blocked** — strict-cascade gate refused. Shows sharing and
   downstream blockers. Each blocking term gets an "Archive this first
   →" button that navigates to that term and auto-expands its archive
   section. The "unwind chain" is tracked in session_state so the
   terminal archive records ``blockers_resolved``.
3. **greenlit** — impact preview ("will move N files: ..."), then the
   reason form + Confirm button.

Public entry point
------------------
``render_archive_section(term_id, term_row, term_s2t) -> None``

Called from ``Business_Glossary.py``'s Term Detail tab.

Session state contract
----------------------
* ``term_sel_detail`` / ``term_sel_spec`` / ``term_sel_dq`` — the three
  sync'd selectbox keys. Setting all three navigates the page.
* ``archive_unwind_target`` — terminal term_id the user is unwinding
  toward. ``None``/absent when no unwind is in flight.
* ``archive_unwind_resolved`` — ordered list of term_ids already
  archived during the current unwind. Passed as ``blockers_resolved``
  when the target finally archives.
* ``archive_auto_expand`` — single-shot flag (popped on read) telling
  the expander to open automatically after navigation.
"""
from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

import archive_term
from archive_dependency_analyzer import (
    ArchiveImpact,
    analyze_archive_impact,
)
from archive_term import (
    AlreadyArchived,
    BlockedArchive,
    run_archive,
)

ARCHIVE_REASON_OPTIONS = [
    "wrong_grain", "bad_definition", "redefined", "obsolete", "other",
]

TERM_SEL_KEYS = ("term_sel_detail", "term_sel_spec", "term_sel_dq")

UNWIND_TARGET_KEY = "archive_unwind_target"
UNWIND_RESOLVED_KEY = "archive_unwind_resolved"
AUTO_EXPAND_KEY = "archive_auto_expand"


# ---------------------------------------------------------------------------
# Session-state helpers (pure — no Streamlit widget calls)
# ---------------------------------------------------------------------------

def _get_resolved_list(state: Any) -> list[str]:
    """Return the current unwind-resolved list, ensuring it is a list
    (not a stale tuple/None) without mutating session state until
    needed."""
    v = state.get(UNWIND_RESOLVED_KEY)
    if isinstance(v, list):
        return v
    return []


def _is_unwinding(state: Any) -> bool:
    return bool(state.get(UNWIND_TARGET_KEY))


def _is_unwind_target(state: Any, term_id: str) -> bool:
    return state.get(UNWIND_TARGET_KEY) == term_id


def _start_unwind(state: Any, target_term_id: str) -> None:
    """Set up a fresh unwind toward ``target_term_id``. Idempotent if
    one is already in flight toward the same target."""
    if state.get(UNWIND_TARGET_KEY) != target_term_id:
        state[UNWIND_TARGET_KEY] = target_term_id
        state[UNWIND_RESOLVED_KEY] = []


def _record_unwind_step(state: Any, archived_term_id: str) -> None:
    """Append a successfully-archived term_id to the resolved list,
    de-duplicating. No-op when no unwind is in flight."""
    if not _is_unwinding(state):
        return
    if archived_term_id == state.get(UNWIND_TARGET_KEY):
        return  # the target itself completes the unwind elsewhere
    resolved = _get_resolved_list(state)
    if archived_term_id not in resolved:
        resolved.append(archived_term_id)
    state[UNWIND_RESOLVED_KEY] = resolved


def _clear_unwind(state: Any) -> None:
    state.pop(UNWIND_TARGET_KEY, None)
    state.pop(UNWIND_RESOLVED_KEY, None)


def _format_chain(resolved: list[str], glossary_df: pd.DataFrame) -> str:
    """Human-friendly rendering of an unwind chain — e.g.
    'BG017 (vendor_concentration_risk) → BG020 (po_volume_by_month)'."""
    if not resolved:
        return "(none yet)"
    name_by_id = dict(zip(glossary_df["id"], glossary_df["term_name"]))
    parts = [f"`{tid}` ({name_by_id.get(tid, '?')})" for tid in resolved]
    return " → ".join(parts)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

def _navigate_to_term(term_name: str) -> None:
    """Switch all three sync'd selectboxes + auto-expand the archive
    section on arrival + rerun. The caller decides whether the
    navigation is part of an unwind step or independent."""
    for k in TERM_SEL_KEYS:
        st.session_state[k] = term_name
    st.session_state[AUTO_EXPAND_KEY] = True
    st.rerun()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def render_archive_section(
    term_id: str,
    term_row: dict[str, Any] | pd.Series,
    term_s2t: pd.DataFrame,
) -> None:
    """Render the archive expander for the current term.

    No-op for terms with no deployed artefacts (``term_s2t.empty``).
    """
    if term_s2t is None or term_s2t.empty:
        return

    term_status = str(term_row.get("status", "") or "").strip()

    # State 1: already archived → audit panel, no expander/button.
    if term_status == "archived":
        _render_already_archived(term_row)
        return

    st.divider()
    auto_expand = bool(st.session_state.pop(AUTO_EXPAND_KEY, False))
    with st.expander("🗄️ Archive this business term", expanded=auto_expand):
        _render_archive_body(term_id, term_row)


# ---------------------------------------------------------------------------
# State-specific renderers
# ---------------------------------------------------------------------------

def _render_already_archived(term_row: Any) -> None:
    arc = str(term_row.get("archive_id", "") or "").strip()
    at = str(term_row.get("archived_at_utc", "") or "").strip()
    reason = str(term_row.get("archived_reason_code", "") or "").strip()
    parts: list[str] = []
    if arc:
        parts.append(f"`{arc}`")
    if at:
        parts.append(f"archived **{at[:10]}**")
    if reason:
        parts.append(f"reason: `{reason}`")
    detail = " · ".join(parts) if parts else "archived"
    st.info(f"🗄️ This term is archived — {detail}.")


def _render_archive_body(term_id: str, term_row: Any) -> None:
    """Inside the expander. Computes impact (with spinner), then routes
    to greenlit or blocked rendering."""
    # If the user is mid-unwind, surface the chain header at the top
    # so they always have context + a cancel hatch.
    _render_unwind_status_bar(term_id)

    # Impact analysis (may run dbt compile — spinner mandatory).
    try:
        with st.spinner("Analysing archive impact (may run dbt compile on first call)..."):
            impact = analyze_archive_impact(term_id)
    except RuntimeError as e:
        st.error(
            f"Could not analyse archive impact: {e}\n\n"
            "Fix the dbt project state and reopen this section."
        )
        return
    except ValueError as e:
        # Term row missing from glossary — shouldn't happen if caller
        # respected the selectbox, but be defensive.
        st.error(f"Cannot archive: {e}")
        return

    if impact.can_archive:
        _render_greenlit(impact, term_id)
    else:
        _render_blocked(impact, term_id)


def _render_unwind_status_bar(term_id: str) -> None:
    """If an unwind is in flight, show its target + resolved chain +
    Cancel button. Visible on every archive screen the user lands on
    during the unwind."""
    if not _is_unwinding(st.session_state):
        return
    target = st.session_state.get(UNWIND_TARGET_KEY)
    resolved = _get_resolved_list(st.session_state)
    try:
        from db import query
        glossary = query("SELECT id, term_name FROM main_seeds.business_glossary")
    except Exception:
        glossary = pd.DataFrame(columns=["id", "term_name"])
    target_name = ""
    if not glossary.empty:
        m = glossary[glossary["id"] == target]
        if not m.empty:
            target_name = str(m.iloc[0]["term_name"])

    if _is_unwind_target(st.session_state, term_id):
        st.success(
            f"📍 You're at the unwind target — `{target}` ({target_name}). "
            "Once any remaining blockers clear, finish the archive here."
        )
    else:
        st.info(
            f"📍 Unwinding toward `{target}` ({target_name}). "
            f"Already archived in this chain: {_format_chain(resolved, glossary)}."
        )
    col_cancel, _ = st.columns([1, 4])
    with col_cancel:
        if st.button("Cancel unwind", key=f"unwind_cancel_{term_id}"):
            _clear_unwind(st.session_state)
            st.rerun()


def _render_greenlit(impact: ArchiveImpact, term_id: str) -> None:
    """The strict-cascade gate is open — show the preview, the reason
    form, and the Confirm button."""
    cascade = impact.exclusive_cascade
    n = len(cascade)
    resolved_chain = _get_resolved_list(st.session_state) if _is_unwind_target(
        st.session_state, term_id
    ) else []

    if n == 0:
        st.warning(
            "This term has no deployed .sql files to archive. "
            "Archiving still records the audit event but moves no files."
        )
    else:
        models_list = ", ".join(f"`{m}`" for m in cascade)
        st.markdown(f"**Will move {n} model file(s):** {models_list}")
    if resolved_chain:
        st.caption(
            "Unwind chain that enabled this archive: "
            + ", ".join(f"`{t}`" for t in resolved_chain)
        )
    st.caption(
        "S2T mapping rows, analysis_findings, and column lineage are "
        "preserved as audit trail (decisions #45, #67)."
    )

    reason_code = st.radio(
        "Reason for archiving *",
        ARCHIVE_REASON_OPTIONS,
        index=None,
        horizontal=True,
        key=f"archive_reason_{term_id}",
    )
    reason_text = st.text_area(
        "Additional context (optional, helps future S2T generations)",
        max_chars=500,
        key=f"archive_text_{term_id}",
    )
    learning_signal = st.checkbox(
        "This archive is a learning signal for future S2T attempts",
        value=True,
        help=(
            "Keep checked (default) if this archive represents a real "
            "lesson learned — future S2T generations will see this context.\n\n"
            "Uncheck for test runs, demo rehearsals, or when you want the "
            "next attempt to reproduce the same result as if this archive "
            "never existed."
        ),
        key=f"archive_learning_{term_id}",
    )

    in_flight = term_id in archive_term._IN_FLIGHT_TERMS
    if st.button(
        "🗄️ Archive Term",
        type="secondary",
        disabled=(reason_code is None) or in_flight,
        key=f"archive_confirm_{term_id}",
    ):
        _execute_archive(
            term_id=term_id,
            reason_code=reason_code,
            reason_text=reason_text or "",
            learning_signal=bool(learning_signal),
            resolved_chain=resolved_chain,
        )


def _render_blocked(impact: ArchiveImpact, term_id: str) -> None:
    """Strict-cascade gate refused. Render sharing + downstream blockers
    with one 'Archive this first →' button per blocking term."""
    n_sharing = len(impact.sharing_blockers)
    n_down = len(impact.downstream_blockers)
    st.error(
        f"**Cannot archive `{term_id}` ({impact.term_name})** — "
        f"{n_sharing} sharing blocker(s), {n_down} downstream blocker(s)."
    )

    # Sharing blockers
    if impact.sharing_blockers:
        st.markdown("**Sharing blockers** — other terms also own the same model:")
        for sb in impact.sharing_blockers:
            st.markdown(f"- `{sb.model_name}` is also used by:")
            for t in sb.other_terms:
                _render_unwind_button(
                    target_term_id=term_id,
                    blocking_term=t,
                    button_key=f"share_{term_id}_{sb.model_name}_{t.term_id}",
                )

    # Downstream blockers
    if impact.downstream_blockers:
        st.markdown(
            "**Downstream blockers** — these models reference yours and would break:"
        )
        for db in impact.downstream_blockers:
            if db.downstream_terms:
                st.markdown(
                    f"- `{db.model_name}` → `{db.downstream_model}` is owned by:"
                )
                for t in db.downstream_terms:
                    _render_unwind_button(
                        target_term_id=term_id,
                        blocking_term=t,
                        button_key=f"down_{term_id}_{db.downstream_model}_{t.term_id}",
                    )
            else:
                st.markdown(
                    f"- `{db.model_name}` → `{db.downstream_model}` — "
                    "no business term owns this downstream model. "
                    "Manual review required (likely system infrastructure)."
                )

    st.caption(
        "Click **Archive this first →** to jump to a blocking term. "
        "When you successfully archive it, you'll be brought back here "
        "and the unwind chain will be tracked automatically."
    )


def _render_unwind_button(
    *,
    target_term_id: str,
    blocking_term,
    button_key: str,
) -> None:
    """One row in the blocker list: term label + 'Archive this first →'
    button. On click: navigate to the blocker, start an unwind toward
    target_term_id."""
    col1, col2 = st.columns([3, 2])
    with col1:
        st.markdown(
            f"&nbsp;&nbsp;&nbsp;&nbsp;`{blocking_term.term_id}` "
            f"({blocking_term.term_name}) — status: `{blocking_term.status}`"
        )
    with col2:
        if st.button(
            "Archive this first →",
            key=button_key,
        ):
            _start_unwind(st.session_state, target_term_id)
            _navigate_to_term(blocking_term.term_name)


# ---------------------------------------------------------------------------
# Saga driver — UI side of run_archive
# ---------------------------------------------------------------------------

def _execute_archive(
    *,
    term_id: str,
    reason_code: str,
    reason_text: str,
    learning_signal: bool,
    resolved_chain: list[str],
) -> None:
    """Call run_archive, handle all three outcomes, advance unwind state,
    emit feedback, navigate."""
    try:
        result = run_archive(
            term_id=term_id,
            reason_code=reason_code,
            reason_text=reason_text,
            learning_signal=learning_signal,
            blockers_resolved=resolved_chain,
        )
    except BlockedArchive as e:
        st.error(
            f"Archive refused at saga gate — "
            f"{len(e.impact.sharing_blockers)} sharing, "
            f"{len(e.impact.downstream_blockers)} downstream blocker(s). "
            "Reload to see the latest state."
        )
        return
    except AlreadyArchived as e:
        st.info(str(e))
        return
    except RuntimeError as e:
        st.error(f"Archive failed: {e}")
        return

    # Success.
    state = st.session_state
    target_id = state.get(UNWIND_TARGET_KEY)

    if _is_unwinding(state) and not _is_unwind_target(state, term_id):
        # Mid-chain archive — record this step, navigate back to target.
        _record_unwind_step(state, term_id)
        try:
            from db import query
            gdf = query("SELECT id, term_name FROM main_seeds.business_glossary")
            target_name_row = gdf[gdf["id"] == target_id]
            target_name = (
                str(target_name_row.iloc[0]["term_name"])
                if not target_name_row.empty else ""
            )
        except Exception:
            target_name = ""
        st.toast(f"Archived `{term_id}` as {result.archive_id}. Returning to `{target_id}`.")
        if target_name:
            _navigate_to_term(target_name)
        else:
            st.rerun()
        return

    # Target archived (or no unwind in progress) — clear state, toast.
    _clear_unwind(state)
    st.toast(
        f"Archived as {result.archive_id}. "
        f"You can now create a new term named '{result.term_name}'."
    )
    try:
        st.rerun()
    except Exception:
        pass
