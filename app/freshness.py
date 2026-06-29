"""Domain-facts freshness gate (Phase 11).

Computes the age gap between the most recent raw-data ingestion
(`main_seeds.ingestion_log.finished_at_utc`) and the oldest active
auto-injectable fact (`main_seeds.domain_facts.evidence_refreshed_at`).

Green (<=3 days) allows everything. Yellow (3-14 days) warns but
allows. Red (>14 days) or no baseline blocks write-path actions
(S2T build, Guided-Domain planning, Domain Report generation). BT
planner always proceeds — stale context beats no context for
read-path analyses. See RULE 31.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st

from db import query

GREEN_MAX_DAYS = 3
YELLOW_MAX_DAYS = 14


def _to_utc_naive(v) -> Optional[datetime]:
    """Best-effort ISO string / pandas timestamp → naive-UTC datetime.

    Returns None for None, NaN, NaT, empty strings, or unparseable values
    so `MAX()` on an empty table (which DuckDB returns as a single-row
    NULL) does not leak through as a spurious baseline.
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if isinstance(v, str) and not v.strip():
        return None
    if isinstance(v, datetime):
        # RULE 36: pandas.Timestamp is a subclass of datetime but its tz
        # methods differ. Coerce to plain datetime up front so the
        # astimezone + replace chain stays in stdlib-datetime semantics.
        dt = v.to_pydatetime() if isinstance(v, pd.Timestamp) else v
    else:
        try:
            dt = pd.to_datetime(v, utc=True).to_pydatetime()
        except Exception:
            return None
    if dt is None or pd.isna(dt):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


@st.cache_data(ttl=60)
def get_last_ingestion_utc() -> Optional[datetime]:
    """MAX(finished_at_utc) from ingestion_log, or None if empty/missing."""
    try:
        # finished_at_utc may be parsed as TIMESTAMP by dbt seed (when all
        # values look like ISO timestamps) or as VARCHAR (on a mixed seed).
        # CAST-to-VARCHAR normalises both so the empty-string guard is safe.
        df = query(
            "SELECT MAX(finished_at_utc) AS ts FROM main_seeds.ingestion_log "
            "WHERE finished_at_utc IS NOT NULL "
            "AND CAST(finished_at_utc AS VARCHAR) != ''"
        )
    except Exception:
        return None
    if df.empty:
        return None
    raw = df.iloc[0].get("ts")
    return _to_utc_naive(raw)


@st.cache_data(ttl=60)
def get_min_evidence_refreshed_utc() -> tuple[Optional[datetime], int]:
    """(MIN(evidence_refreshed_at), count) over active + auto_inject + non-null stale.

    If no qualifying facts exist, returns (None, 0) — caller treats as
    "nothing to be stale".
    """
    try:
        df = query(
            "SELECT MIN(evidence_refreshed_at) AS ts, COUNT(*) AS n "
            "FROM main_seeds.domain_facts "
            "WHERE status = 'active' "
            "  AND auto_inject = TRUE "
            "  AND stale_after_days IS NOT NULL "
            "  AND CAST(stale_after_days AS VARCHAR) != ''"
        )
    except Exception:
        return None, 0
    if df.empty:
        return None, 0
    row = df.iloc[0]
    _raw_n = row.get("n")
    _n = int(_raw_n) if pd.notna(_raw_n) else 0
    return _to_utc_naive(row.get("ts")), _n


def compute_freshness_state() -> dict:
    """Return a dict describing the current freshness state.

    Keys: state, last_ingestion_utc, min_evidence_refreshed_utc, gap_days,
    facts_count, message.
    """
    last_ing = get_last_ingestion_utc()
    min_ref, facts_count = get_min_evidence_refreshed_utc()

    if last_ing is None:
        return {
            "state": "no_baseline",
            "last_ingestion_utc": None,
            "min_evidence_refreshed_utc": min_ref,
            "gap_days": None,
            "facts_count": facts_count,
            "message": (
                "No ingestion baseline recorded. Run "
                "`python scripts/generate_sap_sample_data.py` in a terminal "
                "to stamp a baseline before adding knowledge."
            ),
        }

    if facts_count == 0 or min_ref is None:
        return {
            "state": "green",
            "last_ingestion_utc": last_ing,
            "min_evidence_refreshed_utc": None,
            "gap_days": 0,
            "facts_count": 0,
            "message": "No domain facts yet — nothing to be stale.",
        }

    gap = last_ing - min_ref
    # Negative gap means facts were refreshed AFTER the last ingestion —
    # normal when the user re-captures against the same data load. Clamp
    # so the state logic below treats "newer facts than ingestion" as
    # green.
    gap_days = max(0, gap.days)

    if gap_days <= GREEN_MAX_DAYS:
        state = "green"
        msg = f"Fresh — oldest active fact is {gap_days} day(s) behind latest ingestion."
    elif gap_days <= YELLOW_MAX_DAYS:
        state = "yellow"
        msg = (
            f"Facts are getting stale — oldest active fact is {gap_days} day(s) "
            f"behind latest ingestion. Refresh recommended."
        )
    else:
        state = "red"
        msg = (
            f"Facts are stale — oldest active fact is {gap_days} day(s) "
            f"behind latest ingestion. Re-run ingestion and refresh domain facts."
        )

    return {
        "state": state,
        "last_ingestion_utc": last_ing,
        "min_evidence_refreshed_utc": min_ref,
        "gap_days": gap_days,
        "facts_count": facts_count,
        "message": msg,
    }


def is_write_blocked() -> bool:
    """True iff state is 'red' or 'no_baseline'. Drives button disabled= on
    write-path sites (S2T, Guided-Domain Plan, Domain Report Generate)."""
    return compute_freshness_state()["state"] in {"red", "no_baseline"}


def _fmt_local(dt) -> str:
    """Render a UTC timestamp in the viewer's local timezone.

    Accepts None, NaT, pandas.Timestamp, or datetime.datetime. RULE 36:
    pandas.Timestamp.replace(tzinfo=...) delegates to tz_convert with
    pandas-specific semantics and raises TypeError in the middle of the
    attach→convert chain. Normalise to a plain datetime first, then
    attach UTC explicitly, then astimezone() to local. Split into two
    discrete operations so neither arm has to cope with mixed types.
    """
    if dt is None:
        return "never"
    try:
        if pd.isna(dt):
            return "never"
    except Exception:
        pass
    if isinstance(dt, pd.Timestamp):
        dt = dt.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M")


def render_freshness_banner(location: str) -> None:
    """Streamlit banner for one of {'guided_domain', 's2t', 'domain_report', 'bt'}.

    Shows green/yellow/red/no-baseline state with the age, and a
    'Refresh domain facts now' button on yellow. Red + no_baseline do
    not expose that button — refresh alone cannot fix stale data that
    ingestion hasn't caught up with.
    """
    info = compute_freshness_state()
    state = info["state"]
    last_ing = _fmt_local(info["last_ingestion_utc"])
    min_ref = _fmt_local(info["min_evidence_refreshed_utc"])
    gap = info["gap_days"]

    header_map = {
        "green": ("🟢 Domain facts are fresh", st.success),
        "yellow": ("🟡 Domain facts are getting stale", st.warning),
        "red": ("🔴 Domain facts are stale", st.error),
        "no_baseline": ("🔴 No ingestion baseline recorded", st.error),
    }
    title, renderer = header_map.get(state, ("ℹ️ Domain facts status", st.info))

    detail = (
        f"Last ingestion: **{last_ing}** · "
        f"Oldest active fact refreshed: **{min_ref}** · "
        f"Gap: **{gap if gap is not None else '—'}** day(s) · "
        f"{info['facts_count']} active fact(s)"
    )

    with st.container():
        renderer(f"{title}  \n{info['message']}  \n{detail}")

        # BT never offers the refresh button — it does not gate on state,
        # it just warns. Guided-Domain, S2T, and Domain Report offer the
        # button only when yellow (red needs fresh ingestion first).
        if location != "bt" and state == "yellow":
            key = f"freshness_refresh_{location}"
            if st.button("🔄 Refresh domain facts now", key=key, type="primary"):
                _trigger_refresh_subprocess()


def _trigger_refresh_subprocess() -> None:
    """Shell out to scripts/refresh_domain_facts.py and stream output.

    Narrow exception to "Streamlit does not run pipelines" (decision
    #40 `streamlit_does_not_run_ingestion`): domain-facts refresh is
    knowledge recompute, not data ingestion. The generator still stays
    CLI-only.
    """
    import subprocess
    import sys as _sys
    from pathlib import Path as _PPath

    project_root = _PPath(__file__).resolve().parent.parent
    script = project_root / "scripts" / "refresh_domain_facts.py"
    python_exe = _sys.executable or "python"

    with st.status("Refreshing domain facts...", expanded=True) as status:
        try:
            proc = subprocess.run(
                [python_exe, str(script)],
                capture_output=True, text=True, timeout=600,
                cwd=str(project_root),
            )
        except Exception as e:
            status.update(label="Refresh failed", state="error")
            st.error(f"Subprocess error: {e}")
            return
        if proc.stdout:
            st.code(proc.stdout, language="text")
        if proc.stderr:
            with st.expander("stderr"):
                st.code(proc.stderr, language="text")
        if proc.returncode == 0:
            status.update(label="Refresh complete", state="complete")
            # Bust the freshness cache so the banner re-renders green.
            get_last_ingestion_utc.clear()
            get_min_evidence_refreshed_utc.clear()
            try:
                st.rerun()
            except Exception:
                pass
        else:
            status.update(label=f"Refresh exited rc={proc.returncode}", state="error")
