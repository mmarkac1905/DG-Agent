"""Data Analysis — AI-powered data exploration with knowledge accumulation.

Five tabs:
  - 🧭 Term Scope                — Stage A LLM-driven scope derivation + prereqs
  - 🌐 Domain Analysis           — Stage B per-table analyzers (Run All on selected table; collapsible DAR cells)
  - 🎯 Business Term Analysis    — Stage C Term EDA: prereq grid, grain-relationship pair runner, Term EDA dispatch
  - 🔍 Explore Data              — open-ended NL→SQL Q&A with knowledge reuse
  - 📄 Domain Report             — auto-generated narrative from findings + Q&A + domain facts

Stage D.1 removed the legacy `domain_facts` UI from Domain Analysis
and the legacy `analysis_findings` per-query flow from Business Term Analysis.
The underlying seeds remain queryable via DuckDB for historical audit.
"""
import csv
import json
import os
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

from db import query, get_connection
from dbt_sync import sync_seed
from _term_status_utils import filter_active_terms
from _dar_render import render_dar_card
from _data_analysis_shared import (
    rewrite_sql_for_staging,
    run_query_with_fallback,
    load_actual_staging_schema as _load_actual_staging_schema,
)

# Stage A — import scope-derivation backend
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))
try:
    from _scope_derivation import (  # noqa: E402
        propose_scope as _sd_propose_scope,
        revise_scope as _sd_revise_scope,
        confirm_scope as _sd_confirm_scope,
        load_scope_history as _sd_load_history,
        append_iteration_to_history as _sd_append_iter,
        check_prerequisites as _sd_check_prereqs,
    )
    _SCOPE_DERIVATION_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _SCOPE_DERIVATION_AVAILABLE = False
    _SCOPE_IMPORT_ERR = str(_exc)


def _run_term_eda_subprocess(term_id: str) -> None:
    """Stage C — dispatch scripts/run_term_eda.py as subprocess.

    Timeout 900s (Stage C can run 3-8 LLM turns plus query execution;
    more generous than Stage B's 600s analyzers). Session-state ACKs
    are cleared here for the newly-triggered run so stale ACKs from a
    prior run don't carry over.
    """
    import subprocess as _sp

    _root = Path(__file__).resolve().parent.parent.parent
    _script_path = _root / "scripts" / "run_term_eda.py"
    _cmd = [
        sys.executable, str(_script_path),
        "--term-id", term_id,
        "--executed-by", "analyst",
    ]

    # Clear any prior-run ACKs so a re-run starts fresh.
    for _key in list(st.session_state.keys()):
        if _key.startswith(f"stage_c_ack_{term_id}_"):
            del st.session_state[_key]

    with st.spinner(
        f"Running Term EDA on `{term_id}` (this can take 60-180s; 3+ LLM turns)..."
    ):
        try:
            _proc = _sp.run(
                _cmd,
                capture_output=True,
                text=True,
                timeout=900,
                cwd=str(_root),
            )
        except _sp.TimeoutExpired:
            st.error(
                f"Term EDA on `{term_id}` exceeded 15 minutes. "
                "No TAR rows written (write is atomic at end). Check logs or re-run."
            )
            return
        except FileNotFoundError as _e:
            st.error(f"Dispatch failed ({type(_e).__name__}): {_e}")
            return

    _rc = _proc.returncode
    _stdout = _proc.stdout or ""
    _stderr = _proc.stderr or ""

    if _rc == 0:
        st.success(f"✅ Term EDA on `{term_id}` completed. Results below.")
        with st.expander("runner stdout", expanded=False):
            st.code(_stdout, language="json")
        try:
            from db import close_connection as _close_db
            _close_db()
        except Exception:  # noqa: BLE001
            pass
        try:
            st.cache_data.clear()
        except Exception:  # noqa: BLE001
            pass
        st.rerun()
    else:
        st.error(f"❌ Term EDA on `{term_id}` failed (rc={_rc}).")
        with st.expander("runner stderr", expanded=True):
            st.code(_stderr or "(empty)", language="text")
        with st.expander("runner stdout", expanded=False):
            st.code(_stdout or "(empty)", language="text")


def _render_stage_c_post_run(conn, term_id: str) -> None:
    """Render the latest Stage C run's output for `term_id`: sufficiency
    banner, lens consideration grid, query cards, blockers panel, ACK
    buttons (session-scoped per v5 ACK decision), transition button
    on the escalations-present path."""
    import json as _json

    try:
        _sufficiency_row = conn.execute("""
            SELECT id, sufficiency_json, confidence, executed_at_utc,
                   run_id, status, validation_errors_json
            FROM main_seeds.term_analysis_results
            WHERE term_id = ? AND row_type = 'sufficiency'
              AND status IN ('success', 'quarantined')
            ORDER BY executed_at_utc DESC LIMIT 1
        """, [term_id]).fetchone()
    except Exception as _e:  # noqa: BLE001
        st.caption(f"No TAR output yet for `{term_id}` ({_e}).")
        return
    if not _sufficiency_row:
        st.caption(f"No Term EDA run has completed yet for `{term_id}`.")
        return

    (_suff_id, _suff_json, _confidence, _exec_at, _run_id,
     _suff_status, _validation_errors_json) = _sufficiency_row
    try:
        _sj = _json.loads(_suff_json or "{}")
    except _json.JSONDecodeError:
        _sj = {}

    _declared = bool(_sj.get("declared_sufficient"))
    _blockers_res = _sj.get("blockers_resolution") or []
    _has_escalations = any(
        isinstance(br, dict) and br.get("status") == "escalated_to_analyst"
        for br in _blockers_res
    )
    _has_could_not_resolve = any(
        isinstance(br, dict) and br.get("status") == "could_not_resolve"
        for br in _blockers_res
    )

    _current_status = conn.execute(
        "SELECT status FROM main_seeds.business_glossary WHERE id = ?",
        [term_id],
    ).fetchone()[0]

    st.markdown("#### Sufficiency")
    if _suff_status == "quarantined":
        # KI-108: sufficiency-row banner for quarantined runs. The LLM
        # cited unresolvable tar_ids; KI-109 retry exhausted; writer
        # quarantined the row. Query rows below are validated
        # independently and remain trustworthy.
        _ve = {}
        try:
            _ve = _json.loads(_validation_errors_json or "{}")
        except _json.JSONDecodeError:
            pass
        _err_type = _ve.get("error_type", "validation_error")
        _unresolved = _ve.get("unresolved_ids") or []
        _allocated = _ve.get("allocated_ids_this_run") or []
        _allocated_range = (
            f"`{_allocated[0]}`–`{_allocated[-1]}`"
            if _allocated else "(unknown)"
        )
        _unresolved_preview = ", ".join(_unresolved[:5]) + (
            " …" if len(_unresolved) > 5 else ""
        )
        st.warning(
            f"⚠️ Term EDA run completed but the sufficiency judgment was "
            f"**quarantined** (`{_err_type}`). The LLM cited "
            f"{len(_unresolved)} TAR id(s) outside this run's allocations: "
            f"`{_unresolved_preview}`. Allocated range: {_allocated_range}. "
            f"Query rows below are validated independently and remain "
            f"trustworthy; the sufficiency verdict cannot be relied on "
            f"(KI-102 hallucination class — KI-109 retry exhausted)."
        )
    elif _declared and not _has_escalations and not _has_could_not_resolve:
        st.success(
            f"✅ Term EDA complete — ready for S2T. "
            f"Confidence: **{_confidence or 'unknown'}**. "
            f"Term status: `{_current_status}`."
        )
    elif _has_could_not_resolve or not _declared:
        st.error(
            f"🛑 Term EDA incomplete — confidence: `{_confidence or 'unknown'}`. "
            f"Term status: `{_current_status}`. "
            f"Rationale: {_sj.get('sufficiency_rationale', '')}"
        )
    else:
        _escalation_count = sum(
            1 for br in _blockers_res
            if isinstance(br, dict) and br.get("status") == "escalated_to_analyst"
        )
        st.warning(
            f"⚠️ Term EDA complete — {_escalation_count} escalation(s) "
            f"require analyst acknowledgement. "
            f"Confidence: **{_confidence or 'unknown'}**."
        )

    st.caption(f"Run id: `{_run_id}` · Executed: {_exec_at}")

    # Lens consideration grid
    _lc = _sj.get("lens_consideration") or {}
    if _lc:
        st.markdown("#### Lens consideration (8-lens framework)")
        _lens_cols = st.columns([2, 1, 5])
        _lens_cols[0].markdown("**Lens**")
        _lens_cols[1].markdown("**Decision**")
        _lens_cols[2].markdown("**Rationale**")
        for _lens in (
            "measures_overview", "by_dimension", "ranking", "time_trend",
            "cumulative", "variance", "bucketing", "part_to_whole",
        ):
            _entry = _lc.get(_lens) or {}
            _dec = _entry.get("decision", "—")
            _rat = (_entry.get("rationale") or "")[:300]
            _tar_ids = _entry.get("tar_ids") or []
            _row_cols = st.columns([2, 1, 5])
            _row_cols[0].markdown(f"`{_lens}`")
            _row_cols[1].markdown(
                "✅ picked" if _dec == "picked"
                else "⏭️ skipped" if _dec == "skipped"
                else f"`{_dec}`"
            )
            _rat_str = _rat
            if _tar_ids:
                _rat_str += f"  _(cites: {', '.join(_tar_ids)})_"
            _row_cols[2].markdown(_rat_str)

    # Query rows — cards per row
    try:
        _query_rows = conn.execute("""
            SELECT id, analysis_lens, stage, query_index, query_sql,
                   query_result_json, result_row_count, interpretation,
                   grounded_in_tar_ids, status
            FROM main_seeds.term_analysis_results
            WHERE term_id = ? AND row_type = 'query' AND status IN ('success','error')
              AND run_id = ?
            ORDER BY query_index ASC
        """, [term_id, _run_id]).fetchall()
    except Exception as _e:  # noqa: BLE001
        _query_rows = []

    if _query_rows:
        st.markdown("#### Query rows (this run)")
        for _qr in _query_rows:
            (_qid, _lens, _stage, _qidx, _qsql, _qresult, _qcount, _qinterp,
             _qcites, _qstatus) = _qr
            with st.expander(
                f"TAR {_qid} · lens=`{_lens}` · stage=`{_stage}` · "
                f"rows={_qcount} · status=`{_qstatus}`",
                expanded=False,
            ):
                st.code(_qsql or "(no sql)", language="sql")
                if _qinterp:
                    st.markdown(f"**Interpretation:** {_qinterp}")
                if _qcites:
                    st.caption(f"Grounded in TAR ids: `{_qcites}`")
                if _qresult:
                    with st.expander("result json preview"):
                        _preview = _qresult[:2000]
                        st.code(_preview, language="json")

    # Blockers resolution + ACK buttons
    if _blockers_res:
        st.markdown("#### Blocker resolution")
        for _br in _blockers_res:
            if not isinstance(_br, dict):
                continue
            _bt = _br.get("blocker_short_title", "(unnamed)")
            _bs = _br.get("status", "unknown")
            _bevid = (_br.get("evidence") or "")[:300]
            st.markdown(f"**{_bt}** — `{_bs}`")
            if _bevid:
                st.caption(f"Evidence: {_bevid}")
            if _bs == "escalated_to_analyst":
                _analyst_q = _br.get("analyst_action_needed") or ""
                if _analyst_q:
                    st.markdown(f"_Analyst question:_ {_analyst_q}")
                _ack_key = (
                    f"stage_c_ack_{term_id}_"
                    + _bt.replace(" ", "_").replace("/", "_")[:50]
                )
                if st.session_state.get(_ack_key, False):
                    st.success(f"✓ Acknowledged: {_bt}")
                else:
                    if st.button(
                        f"Acknowledge: {_bt}",
                        key=f"btn_{_ack_key}",
                    ):
                        st.session_state[_ack_key] = True
                        st.rerun()

    # Transition button (escalations-present path only; KI-108: not for quarantined runs)
    if (_suff_status == "success" and _has_escalations and _declared
            and not _has_could_not_resolve):
        # Check all escalations acknowledged
        _all_ack = True
        for _br in _blockers_res:
            if not isinstance(_br, dict):
                continue
            if _br.get("status") != "escalated_to_analyst":
                continue
            _bt = _br.get("blocker_short_title", "")
            _ack_key = (
                f"stage_c_ack_{term_id}_"
                + _bt.replace(" ", "_").replace("/", "_")[:50]
            )
            if not st.session_state.get(_ack_key, False):
                _all_ack = False
                break
        if _all_ack and _current_status != "ready_for_s2t":
            if st.button(
                "▶ Transition to ready_for_s2t",
                key=f"stage_c_transition_{term_id}",
                type="primary",
            ):
                _transition_term_to_ready_for_s2t(term_id)
        elif not _all_ack:
            st.caption(
                "_Transition button appears after all escalations are acknowledged._"
            )


def _transition_term_to_ready_for_s2t(term_id: str) -> None:
    """Rewrite business_glossary.csv to flip term_id → ready_for_s2t, then
    re-seed + parquet sync. Used by the escalations-ACK'd path of Stage C."""
    import csv as _csv

    _bg_csv = Path(__file__).resolve().parent.parent.parent / "dbt" / "seeds" / "business_glossary.csv"
    with _bg_csv.open("r", encoding="utf-8", newline="") as f:
        reader = _csv.DictReader(f)
        rows = list(reader)
        heads = reader.fieldnames
    for row in rows:
        if row.get("id") == term_id:
            row["status"] = "ready_for_s2t"
            break
    tmp = _bg_csv.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(
            f, fieldnames=heads, lineterminator="\n",
            quoting=_csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in heads})
    import os as _os
    _os.replace(tmp, _bg_csv)

    try:
        sys.path.insert(
            0,
            str(Path(__file__).resolve().parent.parent.parent / "scripts"),
        )
        from _parquet_sync import sync_parquet_and_invalidate  # noqa: E402
        sync_parquet_and_invalidate(
            project_root=Path(__file__).resolve().parent.parent.parent,
            seed_name="business_glossary",
            skip=False,
            source="Data_Analysis._transition_term_to_ready_for_s2t",
        )
    except Exception as _e:  # noqa: BLE001
        st.warning(f"parquet sync failed: {_e}")

    st.success(f"Term `{term_id}` transitioned to `ready_for_s2t`.")
    try:
        from db import close_connection as _close_db
        _close_db()
    except Exception:  # noqa: BLE001
        pass
    st.rerun()


def _run_grain_relationship_pairs(
    missing_pairs: list[tuple[str, str]],
) -> None:
    """Stage D.1 — dispatch run_grain_relationship_analysis.py once per
    missing pair, sequentially. Uses st.status() container for progress
    and summary."""
    import subprocess as _sp
    import time as _time

    _root = Path(__file__).resolve().parent.parent.parent
    _script_path = _root / "scripts" / "run_grain_relationship_analysis.py"
    n = len(missing_pairs)
    results: list[dict] = []
    t_start = _time.perf_counter()

    with st.status(
        f"Running grain_relationship on {n} pair(s)...",
        expanded=True,
    ) as status:
        for i, (t1, t2) in enumerate(missing_pairs, start=1):
            pair_str = f"{t1},{t2}"
            status.update(label=f"({i}/{n}) {t1} ↔ {t2}")
            cmd = [
                sys.executable, str(_script_path),
                "--pairs", pair_str,
            ]
            try:
                proc = _sp.run(
                    cmd, capture_output=True, text=True,
                    timeout=600, cwd=str(_root),
                )
                results.append({
                    "pair": (t1, t2),
                    "returncode": proc.returncode,
                    "stderr": proc.stderr or "",
                })
                if proc.returncode == 0:
                    st.markdown(f"- ✅ **{t1} ↔ {t2}**: ok")
                else:
                    st.markdown(
                        f"- ❌ **{t1} ↔ {t2}**: rc={proc.returncode}"
                    )
            except _sp.TimeoutExpired:
                results.append({
                    "pair": (t1, t2),
                    "returncode": -1,
                    "stderr": "timeout (>600s)",
                })
                st.markdown(f"- ⏱️ **{t1} ↔ {t2}**: timeout")
            except Exception as _e:  # noqa: BLE001
                results.append({
                    "pair": (t1, t2),
                    "returncode": -2,
                    "stderr": f"{type(_e).__name__}: {_e}",
                })
                st.markdown(f"- ❌ **{t1} ↔ {t2}**: {type(_e).__name__}")

        elapsed = _time.perf_counter() - t_start
        successes = sum(1 for r in results if r["returncode"] == 0)
        errors_n = sum(1 for r in results if r["returncode"] != 0)
        final_state = "complete" if not errors_n else "error"
        status.update(
            label=(
                f"Grain pairs complete: {successes}✓ / "
                f"{errors_n}✗ / {elapsed:.0f}s"
            ),
            state=final_state,
        )

    try:
        from db import close_connection as _close_db
        _close_db()
    except Exception:  # noqa: BLE001
        pass
    try:
        st.cache_data.clear()
    except Exception:  # noqa: BLE001
        pass

    if errors_n:
        st.error(
            f"{errors_n} pair(s) errored. Inspect stderr in the status "
            "container above; re-click Run All Pairs to retry."
        )
    else:
        st.success(f"✅ All {n} pair analyses completed.")
    st.rerun()


def _run_all_analyzers_for_table(table: str) -> None:
    """Stage D.1 — dispatch 6 per-table DAR analyzers sequentially.

    Uses st.status() container for progress (Streamlit ≥1.29 prereq).
    Sequential not parallel — preserves DuckDB single-writer semantics
    and LLM rate limits. Each analyzer gets its own 600s subprocess
    timeout. Errors are collected per-analyzer; summary shown at end
    with retry affordance.

    performance_baseline is co-emitted from magnitude — no separate run.
    grain_relationship is pairwise and lives in the Business Term
    Analysis tab, not here.
    """
    import subprocess as _sp
    import time as _time

    _root = Path(__file__).resolve().parent.parent.parent

    # Stage F: analyzer list lives in app/_analyzer_registry.py so Data
    # Catalog page's Source Diagnostic UI can dispatch the same suite.
    from _analyzer_registry import SOURCE_DIAGNOSTIC_ANALYZERS
    _RUN_ALL_ANALYZERS = SOURCE_DIAGNOSTIC_ANALYZERS
    n = len(_RUN_ALL_ANALYZERS)

    # Track per-analyzer outcomes for post-run summary + retry.
    results: list[dict] = []
    t_start = _time.perf_counter()

    with st.status(
        f"Running {n} analyzers on `{table}`...",
        expanded=True,
    ) as status:
        for i, (script_rel, label, arg_flavor) in enumerate(
            _RUN_ALL_ANALYZERS, start=1,
        ):
            status.update(label=f"({i}/{n}) {label} on `{table}`")
            script_path = _root / "scripts" / script_rel
            if arg_flavor == "plural":
                cmd = [sys.executable, str(script_path), "--tables", table]
            else:
                cmd = [sys.executable, str(script_path), "--table", table]
            try:
                proc = _sp.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    cwd=str(_root),
                )
                results.append({
                    "label": label,
                    "script_rel": script_rel,
                    "arg_flavor": arg_flavor,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout or "",
                    "stderr": proc.stderr or "",
                })
                if proc.returncode == 0:
                    st.markdown(f"- ✅ **{label}**: ok")
                else:
                    st.markdown(
                        f"- ❌ **{label}**: rc={proc.returncode}"
                    )
            except _sp.TimeoutExpired:
                results.append({
                    "label": label,
                    "script_rel": script_rel,
                    "arg_flavor": arg_flavor,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": "timeout (>600s)",
                })
                st.markdown(f"- ⏱️ **{label}**: timeout")
            except Exception as _e:  # noqa: BLE001
                results.append({
                    "label": label,
                    "script_rel": script_rel,
                    "arg_flavor": arg_flavor,
                    "returncode": -2,
                    "stdout": "",
                    "stderr": f"{type(_e).__name__}: {_e}",
                })
                st.markdown(f"- ❌ **{label}**: {type(_e).__name__}")

        elapsed = _time.perf_counter() - t_start
        successes = sum(1 for r in results if r["returncode"] == 0)
        errors = [r for r in results if r["returncode"] != 0]
        final_state = "complete" if not errors else "error"
        status.update(
            label=(
                f"Run All complete: {successes}✓ / "
                f"{len(errors)}✗ / {elapsed:.0f}s"
            ),
            state=final_state,
        )

    # Persist results for retry + detail display across rerun.
    st.session_state[f"run_all_results_{table}"] = results

    # Post-run parquet + view-catalog refresh.
    try:
        from db import close_connection as _close_db
        _close_db()
    except Exception:  # noqa: BLE001
        pass
    try:
        st.cache_data.clear()
    except Exception:  # noqa: BLE001
        pass

    # Summary with retry buttons (errors only).
    if errors:
        st.error(
            f"{len(errors)} analyzer(s) errored. Inspect stderr and retry "
            f"individually."
        )
        for err_result in errors:
            lbl = err_result["label"]
            with st.expander(
                f"❌ {lbl} — rc={err_result['returncode']}",
                expanded=False,
            ):
                st.code(err_result["stderr"] or "(empty)", language="text")
                if err_result["stdout"]:
                    st.code(err_result["stdout"][-1000:], language="text")
                if st.button(
                    f"▶ Retry {lbl}",
                    key=f"retry_{lbl}_{table}",
                ):
                    _run_analyzer_subprocess(
                        script_rel=err_result["script_rel"],
                        analyzer_label=lbl,
                        table=table,
                        arg_flavor=err_result["arg_flavor"],
                    )
    else:
        st.success(
            f"✅ All {n} analyzers completed successfully on `{table}`."
        )

    st.rerun()


def _run_all_prerequisites(items: list, term_id: str) -> None:
    """Sequential dispatch of all missing-prerequisite items.

    Mirrors `_run_all_analyzers_for_table`'s pattern but iterates a
    heterogeneous list: per-table LLM analyzers + per-pair
    deterministic grain_relationship runs. Items are pre-sorted
    deterministic-first by `_compute_missing_dispatch_items`.

    Each subprocess gets a 600s outer timeout (matches existing
    handlers; internal LLM calls bound themselves at 180s).
    """
    import subprocess as _sp
    import time as _time

    if not items:
        st.warning("No prerequisite items to run.")
        return

    _root = Path(__file__).resolve().parent.parent.parent
    n = len(items)
    results: list = []
    t_start = _time.perf_counter()

    with st.status(
        f"Running {n} prerequisite item(s)...",
        expanded=True,
    ) as status:
        for i, item in enumerate(items, start=1):
            label = item["analyzer"]
            target = item["target_label"]
            status.update(label=f"({i}/{n}) {label} on `{target}`")
            cmd = [
                sys.executable,
                str(_root / item["script_rel"]),
                *item["args"],
            ]
            try:
                proc = _sp.run(
                    cmd, capture_output=True, text=True,
                    timeout=600, cwd=str(_root),
                )
                results.append({
                    "item": item,
                    "returncode": proc.returncode,
                    "stdout": proc.stdout or "",
                    "stderr": proc.stderr or "",
                })
                if proc.returncode == 0:
                    st.markdown(f"- ✅ **{label}** on `{target}`")
                else:
                    st.markdown(
                        f"- ❌ **{label}** on `{target}`: rc={proc.returncode}"
                    )
            except _sp.TimeoutExpired:
                results.append({
                    "item": item,
                    "returncode": -1,
                    "stdout": "",
                    "stderr": "timeout (>600s)",
                })
                st.markdown(f"- ⏱️ **{label}** on `{target}`: timeout")
            except Exception as _e:  # noqa: BLE001
                results.append({
                    "item": item,
                    "returncode": -2,
                    "stdout": "",
                    "stderr": f"{type(_e).__name__}: {_e}",
                })
                st.markdown(
                    f"- ❌ **{label}** on `{target}`: {type(_e).__name__}"
                )

        elapsed = _time.perf_counter() - t_start
        n_ok = sum(1 for r in results if r["returncode"] == 0)
        n_fail = n - n_ok
        final_state = "complete" if n_fail == 0 else "error"
        status.update(
            label=(
                f"Run All Prerequisites: {n_ok}✓ / "
                f"{n_fail}✗ / {elapsed:.0f}s"
            ),
            state=final_state,
        )

    st.session_state[f"prereq_run_results_{term_id}"] = results

    try:
        from db import close_connection as _close_db
        _close_db()
    except Exception:  # noqa: BLE001
        pass
    try:
        st.cache_data.clear()
    except Exception:  # noqa: BLE001
        pass

    if n_fail:
        st.error(
            f"{n_fail} item(s) errored. Inspect the status container "
            "above; re-click Run All Prerequisites to retry remaining "
            "items (already-satisfied items are skipped automatically)."
        )
    else:
        st.success(f"✅ All {n} prerequisite item(s) completed.")
    st.rerun()


def _run_analyzer_subprocess(
    *,
    script_rel: str,
    analyzer_label: str,
    table: str,
    arg_flavor: str,
) -> None:
    """Stage B — dispatch a DAR analyzer CLI as subprocess.

    Timeout policy: 600s bounds the DuckDB-lock-contention
    tail risk. Internal LLM calls already bound themselves at 180s.
    """
    import subprocess as _sp

    _root = Path(__file__).resolve().parent.parent.parent
    _script_path = _root / "scripts" / script_rel
    if arg_flavor == "plural":
        _cmd = [sys.executable, str(_script_path), "--tables", table]
    else:
        _cmd = [sys.executable, str(_script_path), "--table", table]

    with st.spinner(f"Running {analyzer_label} analyzer on `{table}`…"):
        try:
            _proc = _sp.run(
                _cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(_root),
            )
        except _sp.TimeoutExpired:
            st.error(
                f"Analyzer `{analyzer_label}` exceeded the 10-minute bound on "
                f"`{table}`. No DAR row was written (analyzers write atomically "
                f"at end). Check logs or re-run."
            )
            return
        except FileNotFoundError as _e:
            st.error(f"Dispatch failed ({type(_e).__name__}): {_e}")
            return

    _rc = _proc.returncode
    _stdout = _proc.stdout or ""
    _stderr = _proc.stderr or ""

    if _rc == 0:
        st.success(
            f"✅ {analyzer_label} on `{table}` completed. DAR row written."
        )
        with st.expander("analyzer stdout", expanded=False):
            st.code(_stdout, language="text")
        # Refresh parquet-backed views so the status grid sees the new row.
        try:
            from db import close_connection as _close_db
            _close_db()
        except Exception:  # noqa: BLE001
            pass
        try:
            st.cache_data.clear()
        except Exception:  # noqa: BLE001
            pass
        st.rerun()
    else:
        # Exit-code semantics per run_code_tables_analysis.py docstring:
        #   1 = LLM retry exhausted, 2 = SQL retry exhausted, 3 = scope fail.
        _rc_label = {
            1: "LLM retry exhausted",
            2: "SQL execution failed",
            3: "scope resolution failed",
        }.get(_rc, f"exit code {_rc}")
        st.error(
            f"❌ {analyzer_label} on `{table}` failed: {_rc_label}"
        )
        with st.expander("analyzer stderr", expanded=True):
            st.code(_stderr or "(empty)", language="text")
        with st.expander("analyzer stdout", expanded=False):
            st.code(_stdout or "(empty)", language="text")


st.title("🔬 Data Analysis")
st.caption(
    "AI-powered data exploration · Guided analysis for business terms · "
    "Open-ended Q&A · Knowledge accumulation"
)
st.divider()

DB_PATH = Path(__file__).resolve().parent.parent.parent / "cpe_analytics.duckdb"
SEED_DIR = Path(__file__).resolve().parent.parent.parent / "dbt" / "seeds"

# --- Reference data ---
glossary = query("SELECT * FROM main_seeds.business_glossary ORDER BY id")
sap_dict = query("SELECT * FROM main_seeds.sap_data_dictionary ORDER BY table_name, field_name")

# --- Existing knowledge ---
try:
    qa_log = query("SELECT * FROM main_seeds.data_qa_log ORDER BY id DESC")
except Exception:
    qa_log = pd.DataFrame()

try:
    findings = query("SELECT * FROM main_seeds.analysis_findings ORDER BY id DESC")
except Exception:
    findings = pd.DataFrame()


def _save_profiling_cache(sql: str, tables: str, description: str, result_summary: str):
    """Auto-save a profiling query result to data_qa_log with scope='profiling'.

    Keyed by tables_used so the same table profile is not re-planned across terms.
    """
    csv_path = Path(__file__).resolve().parent.parent.parent / "dbt" / "seeds" / "data_qa_log.csv"
    try:
        existing_df = pd.read_csv(csv_path)
        # Skip if we already have a profiling entry for these tables
        if not existing_df.empty and 'scope' in existing_df.columns:
            existing_profiling = existing_df[existing_df['scope'] == 'profiling']
            if not existing_profiling.empty:
                for _, row_existing in existing_profiling.iterrows():
                    if str(row_existing.get('tables_used', '')) == tables:
                        return  # already cached
        max_num = (
            existing_df['id']
            .apply(
                lambda x: int(x.replace('QA', ''))
                if isinstance(x, str) and x.startswith('QA')
                else 0
            )
            .max()
            if not existing_df.empty
            else 0
        )
        max_num = int(max_num or 0)
    except Exception:
        max_num = 0

    fieldnames = [
        'id', 'question', 'generated_sql', 'answer_summary',
        'tables_used', 'key_facts', 'follow_up_context', 'asked_by',
        'asked_date', 'quality_rating', 'scope', 'retry_count', 'original_sql',
    ]
    row = {
        'id': f"QA{max_num + 1:03d}",
        'question': description,
        'generated_sql': sql,
        'answer_summary': result_summary[:500],
        'tables_used': tables,
        'key_facts': '',
        'follow_up_context': '',
        'asked_by': 'auto-profiling',
        'asked_date': pd.Timestamp.now().strftime('%Y-%m-%d'),
        'quality_rating': 'useful',
        'scope': 'profiling',
        'retry_count': '0',
        'original_sql': '',
    }
    _append_csv_row(csv_path, fieldnames, row)


def _append_csv_row(csv_path: Path, fieldnames: list, row: dict):
    """Append a single row to a CSV with LF line endings.

    Using lineterminator='\\n' avoids the mixed CRLF/LF sniffer failure that
    dbt's DuckDB CSV reader hits on Windows.
    """
    file_exists = csv_path.exists()
    with open(csv_path, 'a', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator='\n')
        if not file_exists or csv_path.stat().st_size == 0:
            writer.writeheader()
        writer.writerow({k: row.get(k, '') for k in fieldnames})


DOMAIN_FACTS_FIELDNAMES = [
    'fact_id', 'category', 'scope_layer', 'scope_tables',
    'fact_plain', 'fact_technical', 'evidence_sql',
    'evidence_result_json', 'evidence_result_summary',
    'evidence_refreshed_at', 'discovered_at', 'discovered_by',
    'confidence', 'stale_after_days', 'auto_inject',
    'priority_score', 'status', 'superseded_by',
]

DOMAIN_FACT_CATEGORIES = [
    "currency", "naming_convention", "cardinality", "volume_distribution",
    "null_pattern", "org_structure", "temporal_pattern",
    "referential_integrity", "value_domain", "business_rule_observed",
]


def _next_fact_id() -> str:
    """Return the next `DF-NNNN` id. Falls back to DF-0001 on any error."""
    csv_path = SEED_DIR / "domain_facts.csv"
    try:
        existing = pd.read_csv(csv_path)
        if existing.empty:
            return "DF-0001"
        max_num = (
            existing['fact_id']
            .apply(lambda x: int(str(x).split('-')[-1])
                   if isinstance(x, str) and x.startswith('DF-') else 0)
            .max()
        )
        return f"DF-{int(max_num or 0) + 1:04d}"
    except Exception:
        return "DF-0001"


def _auto_save_domain_fact(
    *,
    edited_sql: str,
    result_df,
    insight: str,
    description: str,
    tables_hint: str,
    focus_area: str,
    selected_domains,
    scope_layer_sel: str,
    session_key: str = "",
):
    """Interpret a Guided-Domain query result, append it to
    `dbt/seeds/domain_facts.csv` with status='active' + auto_inject=true,
    and sync into DuckDB + Parquet via `save_and_sync`. Returns the dict
    row for rendering. On JSON parse failure, synthesises a fallback row
    with confidence='low' and a marker string in fact_plain. Never
    raises — the UI path must stay smooth even when the LLM fails.
    """
    from claude_api import interpret_domain_fact

    rows_json = result_df.head(50).to_json(orient="records")
    row_count = len(result_df)

    interp = interpret_domain_fact(
        sql=edited_sql,
        result_preview=rows_json,
        focus_area=focus_area or "",
        domains=", ".join(selected_domains) if selected_domains else "",
        scope_layer=scope_layer_sel or "staging",
    )

    interp_failed = bool(interp.get("error")) if isinstance(interp, dict) else True

    if interp_failed:
        raw_hint = ""
        if isinstance(interp, dict):
            raw_hint = str(interp.get("raw_response") or interp.get("error") or "")[:400]
        interp = {
            "fact_plain": "[interpretation parse failed, manual review needed]",
            "fact_technical": raw_hint or (insight or description or "")[:500],
            "category": "business_rule_observed",
            "scope_layer": scope_layer_sel or "staging",
            "scope_tables": tables_hint or "",
            "confidence": "low",
            "priority_score": 40,
            "stale_after_days": 30,
        }

    # Normalise any stray LLM keys to the columns we persist.
    def _s(key, default=""):
        v = interp.get(key) if isinstance(interp, dict) else None
        if v is None:
            return default
        return str(v).strip()

    now = pd.Timestamp.now().isoformat(timespec='seconds')
    fact_id = _next_fact_id()
    stale = _s("stale_after_days", "")
    # stale may come back as "null" / "None" / "". Normalise to empty.
    if stale.lower() in ("null", "none"):
        stale = ""

    row = {
        'fact_id': fact_id,
        'category': _s("category", "business_rule_observed"),
        'scope_layer': _s("scope_layer", scope_layer_sel or "staging"),
        'scope_tables': _s("scope_tables", tables_hint or ""),
        'fact_plain': _s("fact_plain"),
        'fact_technical': _s("fact_technical"),
        'evidence_sql': edited_sql,
        'evidence_result_json': rows_json,
        'evidence_result_summary': f"{row_count} rows. {insight}".strip(),
        'evidence_refreshed_at': now,
        'discovered_at': pd.Timestamp.now().strftime('%Y-%m-%d'),
        'discovered_by': 'guided_domain_llm',
        'confidence': _s("confidence", "medium"),
        'stale_after_days': stale,
        'auto_inject': 'true',
        'priority_score': _s("priority_score", "50"),
        'status': 'active',
        'superseded_by': '',
    }

    csv_path = SEED_DIR / "domain_facts.csv"
    _append_csv_row(csv_path, DOMAIN_FACTS_FIELDNAMES, row)
    # sync_seed triggers st.rerun(); stash the row BEFORE the rerun
    # so the next run can render the fact card from session state.
    if session_key:
        st.session_state[session_key] = row
    sync_seed(
        "domain_facts",
        success_msg=f"✅ {fact_id} saved. Auto-injecting into future prompts.",
    )
    return row


def _delete_domain_fact(fact_id: str) -> bool:
    """Remove a fact row by id, re-sync seed to DuckDB+Parquet.

    Returns True on success. Caller handles st.toast / messaging.
    """
    csv_path = SEED_DIR / "domain_facts.csv"
    try:
        df = pd.read_csv(csv_path)
        before = len(df)
        df = df[df['fact_id'] != fact_id]
        if len(df) == before:
            return False
        df.to_csv(csv_path, index=False, lineterminator='\n')
        sync_seed("domain_facts", success_msg=f"🗑️ Deleted {fact_id}")
        return True
    except Exception as e:
        st.error(f"Delete failed: {e}")
        return False


def _render_fact_card(fact: dict, *, key_suffix: str):
    """Render the auto-saved fact as a compact card with a Delete button.

    `key_suffix` disambiguates Streamlit widget keys when multiple cards
    appear on the page simultaneously.
    """
    fid = str(fact.get('fact_id', ''))
    with st.container():
        st.markdown("---")
        st.markdown(f"##### 💡 Auto-saved: **{fid}**")
        st.markdown(f"**Plain:** {fact.get('fact_plain', '')}")
        tech = str(fact.get('fact_technical', '') or '').strip()
        if tech:
            st.caption(f"Technical: {tech}")
        st.caption(
            f"category=`{fact.get('category', '')}` · "
            f"scope_layer=`{fact.get('scope_layer', '')}` · "
            f"scope_tables=`{fact.get('scope_tables', '')}` · "
            f"confidence=`{fact.get('confidence', '')}` · "
            f"priority=`{fact.get('priority_score', '')}` · "
            f"stale_after_days=`{fact.get('stale_after_days', '')}`"
        )
        _col_spacer, _col_delete = st.columns([5, 1])
        with _col_delete:
            if st.button("🗑️ Delete this fact", key=f"gd_delete_{fid}_{key_suffix}"):
                if _delete_domain_fact(fid):
                    # Clear any session-state card pointers referencing this id.
                    for k in list(st.session_state.keys()):
                        if k.startswith("gd_fact_saved_") and isinstance(st.session_state[k], dict) \
                                and st.session_state[k].get('fact_id') == fid:
                            st.session_state.pop(k, None)
                    try:
                        st.toast(f"Deleted {fid}")
                    except Exception:
                        pass


# ─── Stage A UI helpers ───

def _render_prereq_section(prereq) -> None:
    """Render PrerequisitesStatus dataclass in the Term Scope tab."""
    st.markdown("### Scope tables & domain EDA status")
    if prereq.scope_tables:
        _rows = []
        for t in prereq.scope_tables:
            ok = prereq.domain_eda_status.get(t, False)
            _rows.append({
                "table": t,
                "domain_eda": "✅ ready" if ok else "⚠️ needed",
            })
        st.dataframe(pd.DataFrame(_rows), hide_index=True,
                     use_container_width=True)
    else:
        st.warning("No scope tables recorded for this term.")

    st.markdown("### Stage status")
    st.write(f"- **Term EDA**: `{prereq.term_eda_status}`")
    st.write(f"- **S2T readiness**: `{prereq.s2t_readiness}`")

    st.markdown("### Next steps")
    if prereq.next_steps:
        for ns in prereq.next_steps:
            st.markdown(f"- {ns}")
    else:
        st.write("_(no immediate next steps)_")


def _render_proposal_section(term_id, latest_iter, iterations, conn) -> None:
    """Render the active proposal + re-prompt controls + confirm button."""
    llm = latest_iter.get("llm_response") or {}
    tables = llm.get("proposed_tables") or []
    rat = llm.get("rationale_per_table") or {}
    blockers = llm.get("blockers") or []
    confidence = llm.get("confidence") or "?"
    conf_rationale = llm.get("confidence_rationale") or ""
    validation_issues = latest_iter.get("validation_issues") or []

    st.markdown(
        f"**Current proposal — iteration {latest_iter.get('iter_num')} "
        f"({latest_iter.get('mode')}, confidence: `{confidence}`)**"
    )
    if conf_rationale:
        st.caption(conf_rationale)

    if validation_issues:
        st.warning("Validation issues: " + "; ".join(validation_issues))

    # Tables + rationale
    st.markdown("### Proposed scope")
    if tables:
        _rows = [
            {"table": t, "rationale": rat.get(t, "")} for t in tables
        ]
        st.dataframe(pd.DataFrame(_rows), hide_index=True,
                     use_container_width=True)
    else:
        st.error("LLM returned empty scope.")

    # Join path
    join_path = llm.get("join_path") or []
    if join_path:
        st.markdown("### Join path")
        st.dataframe(pd.DataFrame(join_path), hide_index=True,
                     use_container_width=True)

    # Blockers — cross-stage contract. 6 augmentation fields drive
    # the structured view; older proposals render via backward-compat path.
    if blockers:
        st.markdown("### Blockers")
        _RESOLVES_BADGE = {
            "domain_eda":         "🔍 domain_eda",
            "term_eda":           "🧩 term_eda",
            "analyst_decision":   "👤 analyst_decision",
            "ingestion_required": "📥 ingestion_required",
        }
        for b in blockers:
            btype = b.get("type", "?")
            btables = b.get("tables", [])
            title = b.get("short_title") or b.get("note") or btype
            is_legacy = not b.get("short_title")

            tables_tag = f" · `{', '.join(btables)}`" if btables else ""
            resolves_in = b.get("resolves_in") or ""
            badge = _RESOLVES_BADGE.get(resolves_in, "")
            badge_tag = f" · {badge}" if badge else ""

            st.warning(f"⚠ **{title}**  ·  `{btype}`{tables_tag}{badge_tag}")

            with st.expander("Details"):
                if is_legacy:
                    st.caption(
                        "_(legacy proposal — pre-augmentation format; "
                        "fresh proposals include structured detail fields)_"
                    )
                    if b.get("note"):
                        st.markdown(f"**Note:** {b['note']}")
                else:
                    if b.get("what_it_means"):
                        st.markdown(f"**What it means:** {b['what_it_means']}")
                    if b.get("what_llm_needs"):
                        st.markdown(
                            f"**What the system needs:** {b['what_llm_needs']}"
                        )
                    if resolves_in and b.get("resolves_via"):
                        st.markdown(
                            f"**Resolves in** `{resolves_in}` — "
                            f"{b['resolves_via']}"
                        )
                    if b.get("user_action_now"):
                        st.info(f"**What to do now:** {b['user_action_now']}")

    # Re-prompt controls (composed instruction string approach)
    with st.expander("↻ Revise proposal (re-prompt LLM)"):
        st.markdown("Any combination of the inputs below is sent as a "
                    "single composed instruction.")
        _other_tables = sorted(
            _live_raw_sap_tables(conn) - {t.lower() for t in tables}
        )
        add_t = st.selectbox("Add table (optional)",
                             options=["(none)"] + _other_tables,
                             key=f"scope_add_{term_id}")
        rm_t = st.selectbox("Remove table (optional)",
                            options=["(none)"] + sorted(tables),
                            key=f"scope_rm_{term_id}")
        exp_t = st.selectbox("Explain table (optional)",
                             options=["(none)"] + sorted(tables),
                             key=f"scope_exp_{term_id}")
        free = st.text_area("Free-text instruction (optional)",
                            height=80, key=f"scope_free_{term_id}")
        if st.button("🔄 Revise Proposal", key=f"btn_revise_{term_id}"):
            parts = []
            if add_t != "(none)":
                parts.append(f"Add table {add_t}.")
            if rm_t != "(none)":
                parts.append(f"Remove table {rm_t}.")
            if exp_t != "(none)":
                parts.append(f"Explain table {exp_t} in more detail.")
            if free.strip():
                parts.append(f"Additional: {free.strip()}")
            instruction = " ".join(parts).strip()
            if not instruction:
                st.error("Provide at least one revision input.")
            else:
                with st.spinner("Revising proposal..."):
                    try:
                        _prop = _sd_revise_scope(
                            term_id, instruction, conn=None,
                        )
                        _sd_append_iter(term_id, _prop)
                        st.success(
                            f"Revised (iter {_prop.iter_num})."
                        )
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Revise failed: {e}")

    # Confirm button
    st.markdown("---")
    _confirmed_by_default = os.environ.get("USER", "analyst")
    _by = st.text_input("Confirmed by (your name/id)",
                        value=_confirmed_by_default,
                        key=f"scope_confirmed_by_{term_id}")
    if st.button("✅ Confirm this Scope",
                 key=f"btn_confirm_{term_id}",
                 type="primary"):
        if validation_issues:
            st.error("Cannot confirm a proposal with validation issues. "
                     "Revise first.")
        else:
            with st.spinner("Writing scope + updating status..."):
                try:
                    res = _sd_confirm_scope(
                        term_id, latest_iter.get("iter_num"), _by, conn=None,
                    )
                    if res.success:
                        st.success(
                            f"Scope confirmed. {res.s2t_rows_written} "
                            f"s2t_mapping rows written. "
                            f"status -> scope_confirmed."
                        )
                        st.rerun()
                    else:
                        st.error(f"Confirm failed: {res.error}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Confirm threw: {e}")

    # History viewer
    with st.expander(f"View full derivation history "
                     f"({len(iterations)} iterations)"):
        st.json({"iterations": iterations})


def _live_raw_sap_tables(conn) -> set:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='raw_sap'"
    ).fetchall()
    return {r[0].lower() for r in rows}


# Stage A — Term Scope tab added as FIRST tab. Note for
# returning users: default landing is no longer "Business Term
# Analysis"; Term Scope is the new gateway for draft terms.
# Tab order (post-Stage-B): Term Scope → Domain Analysis → Business
# Term Analysis. Variable names below preserved (tab_domain still maps
# to the Domain Analysis tab, tab_guided to Business Term Analysis) so
# the existing `with tab_X:` blocks below render the correct content.
tab_scope, tab_domain, tab_guided, tab_explore, tab_report = st.tabs([
    "🧭 Term Scope",
    "🌐 Domain Analysis",
    "🎯 Business Term Analysis",
    "🔍 Explore Data",
    "📄 Domain Report",
])


# ============================================================
# TAB 0: TERM SCOPE — Stage A scope derivation + prerequisites readout
# ============================================================
with tab_scope:
    st.subheader("🧭 Term Scope & Prerequisites")
    st.caption(
        f"Stage A: LLM proposes the set of {os.environ.get('DG_SOURCE_SCHEMA', 'raw_sap')} "
        "tables a business term "
        "needs, you revise and confirm. Confirmed scope writes to "
        "`s2t_mapping` and transitions the term to `scope_confirmed`. "
        "Prerequisites readout shows what's needed before S2T can run."
    )

    if not _SCOPE_DERIVATION_AVAILABLE:
        st.error(
            f"Scope derivation backend unavailable: {_SCOPE_IMPORT_ERR}"
        )
    else:
        _conn_scope = get_connection()

        # ─── Section 1: Term selector ───
        _bg_rows = _conn_scope.execute(
            "SELECT id, term_name, status FROM main_seeds.business_glossary "
            "WHERE status != 'archived' ORDER BY id"
        ).fetchall()
        _options = ["(none selected)"] + [
            f"{r[0]} — {r[1]} [{r[2]}]" for r in _bg_rows
        ]
        _picked = st.selectbox(
            "Select business term",
            options=_options, index=0, key="scope_term_picker",
        )
        _sel_term_id = None
        if _picked != "(none selected)":
            _sel_term_id = _picked.split(" — ")[0]

        if _sel_term_id is None:
            st.info("Select a business term above to view its scope status.")
        else:
            # ─── Section 2: per-term state-dependent rendering ───
            _term_row = next(
                (r for r in _bg_rows if r[0] == _sel_term_id), None,
            )
            _term_status = _term_row[2] if _term_row else "?"
            _term_name = _term_row[1] if _term_row else _sel_term_id

            st.markdown(
                f"**{_sel_term_id}** — {_term_name} "
                f"(status: `{_term_status}`)"
            )

            _history = _sd_load_history(_sel_term_id, conn=_conn_scope)
            _iterations = _history.get("iterations", []) if _history else []
            _latest_iter = _iterations[-1] if _iterations else None

            # Case A: legacy approved — no re-derive, just show prereqs
            if _term_status == "approved":
                st.info(
                    "This term was scope-set via the legacy flow. Stage A "
                    "does not re-derive approved terms. To re-derive, reset "
                    "the term to `draft` in Business Glossary first."
                )
                _prereq = _sd_check_prereqs(_sel_term_id, conn=_conn_scope)
                _render_prereq_section(_prereq)

            # Case B: draft, no history → Propose Scope button
            elif _term_status == "draft" and not _iterations:
                st.markdown(
                    "No scope derivation yet. Click below to propose a "
                    "scope via the LLM."
                )
                if st.button("🤖 Propose Scope with LLM",
                             key=f"btn_propose_{_sel_term_id}"):
                    with st.spinner(
                        "Analyzing term definition against data catalog..."
                    ):
                        try:
                            _prop = _sd_propose_scope(
                                _sel_term_id, conn=None,  # LLM call writes
                            )
                            _sd_append_iter(_sel_term_id, _prop)
                            st.success(
                                f"Proposal generated (iter {_prop.iter_num}). "
                                f"Review below."
                            )
                            st.rerun()
                        except Exception as e:  # noqa: BLE001
                            st.error(f"Propose failed: {e}")

            # Case C: draft, has history, not yet confirmed
            elif _term_status == "draft" and _iterations:
                _render_proposal_section(
                    _sel_term_id, _latest_iter, _iterations,
                    _conn_scope,
                )

            # Case D: scope_confirmed (or reserved intermediate statuses)
            elif _term_status in ("scope_confirmed", "domain_eda_pending",
                                  "term_eda_pending", "ready_for_s2t"):
                st.success(
                    f"Scope confirmed on {_history.get('confirmed_at_utc', '?')} "
                    f"by {_history.get('confirmed_by', '?')}."
                )
                _prereq = _sd_check_prereqs(_sel_term_id, conn=_conn_scope)
                _render_prereq_section(_prereq)
                with st.expander("View full derivation history"):
                    st.json(_history)

            else:
                st.warning(f"Unhandled term status: `{_term_status}`")



# ============================================================
# TAB 1: BUSINESS TERM ANALYSIS — Stage C Term EDA
# ============================================================
with tab_guided:

    # ============================================================
    # SECTION 1 (NEW) — 🎯 Term EDA (Stage C)
    # 8-lens EDA framework, knowledge reuse, sufficiency loop.
    # ============================================================
    st.subheader("🎯 Term EDA")
    st.caption(
        "Run the 8-lens EDA framework against a term's confirmed scope "
        "to produce grounding evidence for S2T authoring. "
        "Prior TARs from other terms that share scope tables are surfaced "
        "for citation; new queries execute only when knowledge gaps remain."
    )

    try:
        from _term_eda_prereq import check_term_eda_prereq as _stage_c_prereq  # noqa: E402
        from _prereq_dispatch import compute_missing_dispatch_items  # noqa: E402
        _STAGE_C_AVAILABLE = True
    except Exception as _exc:  # noqa: BLE001
        _STAGE_C_AVAILABLE = False
        _STAGE_C_IMPORT_ERR = str(_exc)

    if not _STAGE_C_AVAILABLE:
        st.error(f"Stage C backend unavailable: {_STAGE_C_IMPORT_ERR}")
    else:
        _conn_stage_c = get_connection()

        # Term selector — scope_confirmed+ only; draft/approved/archived excluded.
        try:
            _stage_c_term_rows = _conn_stage_c.execute("""
                SELECT id, term_name, status
                FROM main_seeds.business_glossary
                WHERE status IN (
                    'scope_confirmed', 'domain_eda_pending',
                    'term_eda_pending', 'ready_for_s2t'
                )
                ORDER BY id
            """).fetchall()
        except Exception as _e:  # noqa: BLE001
            _stage_c_term_rows = []
            st.warning(f"Could not load eligible terms: {_e}")

        _stage_c_placeholder = "— select a term —"
        _stage_c_options = [_stage_c_placeholder] + [
            f"{r[0]} — {r[1]} [{r[2]}]" for r in _stage_c_term_rows
        ]
        _stage_c_choice = st.selectbox(
            "Scope-confirmed term",
            options=_stage_c_options,
            index=0,
            key="stage_c_term_dropdown",
            help="Terms with status in {scope_confirmed, domain_eda_pending, "
                 "term_eda_pending, ready_for_s2t}.",
        )
        _stage_c_selected = None if _stage_c_choice == _stage_c_placeholder \
            else _stage_c_choice.split(" — ", 1)[0]

        if _stage_c_selected:
            _prereq = _stage_c_prereq(_conn_stage_c, _stage_c_selected)
            # Prerequisites panel
            st.markdown(f"### Term: `{_stage_c_selected}`")
            if _prereq["ready"]:
                st.success(
                    f"✅ Prerequisites met. "
                    f"Scope: `{', '.join(_prereq['scope_tables'])}`. "
                    f"Current status: `{_prereq['current_status']}`."
                )
            else:
                st.warning(
                    f"⚠️ Prerequisites not met — reason: "
                    f"`{_prereq['reason']}` "
                    f"(status: `{_prereq['current_status']}`)"
                )

                # === Run All Prerequisites (one-click batch dispatch) ===
                _ra_items = compute_missing_dispatch_items(_prereq)
                if _ra_items:
                    _ra_n = len(_ra_items)
                    _ra_total_cost = sum(
                        _it["est_cost_usd"] for _it in _ra_items
                    )
                    _ra_total_max_s = sum(
                        _it["est_seconds_max"] for _it in _ra_items
                    )
                    _ra_minutes = max(1, round(_ra_total_max_s / 60))
                    _ra_button_label = (
                        f"▶ Run All Prerequisites "
                        f"({_ra_n} items, "
                        f"~${_ra_total_cost:.2f}, "
                        f"~{_ra_minutes}m)"
                    )
                    _ra_confirm_key = f"prereq_confirm_{_stage_c_selected}"

                    if not st.session_state.get(_ra_confirm_key, False):
                        if _ra_total_cost == 0:
                            # Zero-cost shortcut: dispatch directly.
                            if st.button(
                                _ra_button_label,
                                key=f"prereq_run_{_stage_c_selected}",
                            ):
                                _run_all_prerequisites(
                                    _ra_items, _stage_c_selected,
                                )
                        else:
                            # Cost > 0: stash flag, show confirm
                            # expander on next rerun.
                            if st.button(
                                _ra_button_label,
                                key=f"prereq_run_{_stage_c_selected}",
                            ):
                                st.session_state[_ra_confirm_key] = True
                                st.rerun()
                    else:
                        with st.expander(
                            f"⚠️ Confirm: run {_ra_n} prerequisite "
                            f"item(s) (~${_ra_total_cost:.2f}, "
                            f"~{_ra_minutes}m)",
                            expanded=True,
                        ):
                            st.markdown(
                                "**Items to run** (deterministic-first):"
                            )
                            for _it in _ra_items:
                                _kind_marker = (
                                    "deterministic"
                                    if _it["is_deterministic"]
                                    else "LLM"
                                )
                                st.markdown(
                                    f"- `{_it['analyzer']}` on "
                                    f"`{_it['target_label']}` "
                                    f"({_kind_marker})"
                                )
                            _cc1, _cc2 = st.columns(2)
                            with _cc1:
                                if st.button(
                                    "✅ Confirm & Run",
                                    key=(
                                        "prereq_confirm_run_"
                                        f"{_stage_c_selected}"
                                    ),
                                ):
                                    st.session_state[_ra_confirm_key] = False
                                    _run_all_prerequisites(
                                        _ra_items, _stage_c_selected,
                                    )
                            with _cc2:
                                if st.button(
                                    "❌ Cancel",
                                    key=(
                                        "prereq_cancel_"
                                        f"{_stage_c_selected}"
                                    ),
                                ):
                                    st.session_state[_ra_confirm_key] = False
                                    st.rerun()

                # Stage D.1 Part 3.7 — per-table × per-analyzer grid.
                _canonical_analyzers = [
                    "completeness", "dimensions", "magnitude", "code_tables",
                    "date", "segmentation", "grain_relationship",
                    "performance_baseline",
                ]
                _scope_tbls = _prereq.get("scope_tables") or []
                _missing_map = _prereq.get("missing_analyzers_per_table") or {}
                if _scope_tbls:
                    st.markdown("**Analyzer coverage per scope table:**")
                    _grid_data: dict[str, list[str]] = {}
                    for _tbl in _scope_tbls:
                        _miss = set(_missing_map.get(_tbl, []))
                        _grid_data[_tbl] = [
                            "✗" if a in _miss else "✓"
                            for a in _canonical_analyzers
                        ]
                    _grid_df = pd.DataFrame(
                        _grid_data, index=_canonical_analyzers,
                    ).T
                    st.dataframe(_grid_df, use_container_width=True)

                # Missing grain pairs (if any).
                _missing_pairs = _prereq.get("missing_grain_pairs") or []
                if _missing_pairs:
                    st.markdown("**Missing grain-relationship pairs:**")
                    for _t1, _t2 in _missing_pairs:
                        st.markdown(f"- `{_t1}` ↔ `{_t2}`")
                    st.caption(
                        "_Use the Grain Relationships section below to run "
                        "pair analyses._"
                    )

                st.markdown("**Next steps:**")
                for _step in _prereq["next_steps"]:
                    st.markdown(f"- {_step}")

            # Stage D.1 Part 3.5 — Grain Relationships sub-section.
            _scope_for_grain = _prereq.get("scope_tables") or []
            if len(_scope_for_grain) >= 2:
                import itertools as _it
                st.markdown("### Grain Relationships")
                st.caption(
                    "Pairwise analyses required for Term EDA prereq. "
                    f"{len(_scope_for_grain)} scope tables → "
                    f"{len(list(_it.combinations(sorted(_scope_for_grain), 2)))} pairs."
                )
                _all_pairs = list(_it.combinations(sorted(_scope_for_grain), 2))
                _missing_pairs_list: list[tuple[str, str]] = []
                for _t1, _t2 in _all_pairs:
                    _pair_key = f"{_t1},{_t2}"
                    try:
                        _pair_dar_row = _conn_stage_c.execute(
                            "SELECT status, result_json, query_sql, "
                            "executed_at_utc, analysis_type "
                            "FROM main_seeds.domain_analysis_results "
                            "WHERE source_tables = ? "
                            "AND analysis_type = 'grain_relationship' "
                            "AND status IN ('success', 'skipped') "
                            "ORDER BY executed_at_utc DESC LIMIT 1",
                            [_pair_key],
                        ).fetchone()
                    except Exception:  # noqa: BLE001
                        _pair_dar_row = None

                    if _pair_dar_row is None:
                        _missing_pairs_list.append((_t1, _t2))
                        st.markdown(
                            f"**✗ `{_t1}` ↔ `{_t2}`** — not yet run"
                        )
                    else:
                        _p_status, _p_rj, _p_sql, _p_at, _p_atype = _pair_dar_row
                        _icon = "✓" if _p_status == "success" else "✓"
                        _text = (
                            "Success" if _p_status == "success"
                            else "Skipped (see collapsible)"
                        )
                        st.markdown(
                            f"**{_icon} `{_t1}` ↔ `{_t2}`** — {_text}"
                        )
                        with st.expander("▸ View results", expanded=False):
                            render_dar_card({
                                "analysis_type": _p_atype,
                                "status": _p_status,
                                "query_sql": _p_sql or "",
                                "result_json": _p_rj or "",
                                "executed_at_utc": str(_p_at) if _p_at else "",
                            }, st)

                if _missing_pairs_list:
                    if st.button(
                        f"▶ Run Grain Relationships on All Pairs "
                        f"({len(_missing_pairs_list)} missing)",
                        key=f"run_grain_pairs_{_stage_c_selected}",
                    ):
                        _run_grain_relationship_pairs(_missing_pairs_list)
                else:
                    st.success(
                        f"✓ All {len(_all_pairs)} pairs covered"
                    )
            elif len(_scope_for_grain) == 1:
                st.info(
                    "Single-table scope — grain_relationship "
                    "auto-satisfied; no pair analyses needed."
                )

            # Prior TAR count preview (knowledge reuse visibility)
            try:
                sys.path.insert(
                    0,
                    str(Path(__file__).resolve().parent.parent.parent / "scripts"),
                )
                from _tar_corpus_loader import load_candidate_prior_tars  # noqa: E402
                _prior_tars = load_candidate_prior_tars(
                    _conn_stage_c, _stage_c_selected,
                )
                st.caption(
                    f"Prior TAR candidates for citation: **{len(_prior_tars)}** "
                    f"(from other terms with scope-table overlap)."
                )
            except Exception as _e:  # noqa: BLE001
                st.caption(f"Prior TAR count unavailable: {_e}")

            # Run button (gated on prereqs)
            _run_disabled = not _prereq["ready"]
            if st.button(
                "▶ Run Term EDA",
                key=f"stage_c_run_{_stage_c_selected}",
                type="primary",
                disabled=_run_disabled,
                help="Dispatches scripts/run_term_eda.py as subprocess. "
                     "~60-180s runtime (3+ LLM turns + query execution).",
            ):
                _run_term_eda_subprocess(_stage_c_selected)

            # Post-run display — latest TAR rows
            _render_stage_c_post_run(_conn_stage_c, _stage_c_selected)


# ============================================================
# TAB 2: DOMAIN ANALYSIS — Stage B per-table analyzer dispatch
# ============================================================
with tab_domain:
    import json as _dj

    # ============================================================
    # SECTION 1 (NEW) — Per-Table Domain EDA
    # Stage B: per-table analyzer dispatch with automatic
    # Stage A blocker injection. Analysts arrive here from the Term
    # Scope tab's next_steps hints.
    # ============================================================
    st.subheader("🎯 Per-Table Domain EDA")
    st.caption(
        "Run domain analyzers on a scope table. Stage A blockers with "
        "`resolves_in='domain_eda'` targeting the selected table are "
        "injected into analyzer prompts automatically."
    )

    # Load Stage A blocker helpers (sys.path was set up at module top).
    try:
        from _stage_a_blocker_loader import (  # noqa: E402
            load_blockers_for_table as _sb_load_blockers,
        )
        _STAGE_B_LOADER_OK = True
    except Exception as _sb_exc:  # noqa: BLE001
        _STAGE_B_LOADER_OK = False
        _STAGE_B_LOADER_ERR = str(_sb_exc)

    if not _STAGE_B_LOADER_OK:
        st.error(f"Stage B blocker loader unavailable: {_STAGE_B_LOADER_ERR}")
    else:
        _conn_sb = get_connection()

        # Populate table dropdown from s2t_mapping rows whose business_term_id
        # has an eligible status. Deduped, sorted.
        try:
            _sb_tables = [r[0] for r in _conn_sb.execute("""
                SELECT DISTINCT LOWER(s.source_table) AS t
                FROM main_seeds.s2t_mapping s
                JOIN main_seeds.business_glossary bg ON bg.id = s.business_term_id
                WHERE bg.status IN (
                    'scope_confirmed', 'domain_eda_pending',
                    'term_eda_pending', 'ready_for_s2t'
                )
                ORDER BY 1
            """).fetchall()]
        except Exception as _sb_e:  # noqa: BLE001
            _sb_tables = []
            st.warning(f"Could not load eligible tables: {_sb_e}")

        _sb_placeholder = "— select a table —"
        _sb_sel_col, _sb_run_all_col = st.columns([3, 1])
        with _sb_sel_col:
            _sb_dropdown = st.selectbox(
                "Scope table",
                options=[_sb_placeholder] + _sb_tables,
                index=0,
                key="stage_b_table_dropdown",
                help=(
                    "Tables in s2t_mapping for any term with status in "
                    "{scope_confirmed, domain_eda_pending, term_eda_pending, "
                    "ready_for_s2t}."
                ),
            )
        _sb_selected = None if _sb_dropdown == _sb_placeholder else _sb_dropdown
        with _sb_run_all_col:
            # Vertical padding so the button aligns with the selectbox.
            st.markdown("&nbsp;")
            _sb_run_all_clicked = st.button(
                "▶ Run All Analyzers on This Table",
                disabled=(_sb_selected is None),
                key="stage_b_run_all",
                help=(
                    "Sequentially runs 6 per-table analyzers "
                    "(completeness, dimensions, magnitude, code_tables, "
                    "date, segmentation). performance_baseline "
                    "co-emits from magnitude. grain_relationship is "
                    "pairwise — run from the Business Term Analysis tab."
                ),
            )

        if _sb_selected:
            st.markdown(f"### Table: `{_sb_selected}`")

            # ── Pending blockers panel ──
            # KI-114: filter_resolved=True excludes blockers Stage C
            # marked resolved/not_applicable so the panel shows only
            # blockers still needing analyst attention.
            _sb_entries, _sb_trunc = _sb_load_blockers(
                _sb_selected, filter_resolved=True,
            )
            _sb_bl_count = len(_sb_entries)
            if _sb_bl_count == 0:
                with st.expander(
                    "✅ No blockers targeting this table",
                    expanded=False,
                ):
                    st.caption(
                        "No Stage A blockers with `resolves_in='domain_eda'` "
                        "reference this table across any eligible term. "
                        "Running analyzers produces baseline DAR rows; the "
                        "injected concerns section in their prompts will be "
                        "empty."
                    )
            else:
                _sb_header = (
                    f"⚠️ {_sb_bl_count} blocker(s) pending for this table"
                )
                with st.expander(_sb_header, expanded=True):
                    for _i, _e in enumerate(_sb_entries, start=1):
                        _b = _e.get("blocker") or {}
                        st.markdown(
                            f"**Concern {_i} — {_e.get('term_id','')} "
                            f"({_e.get('term_name','')})**"
                        )
                        _cols_sb = st.columns([1, 2])
                        with _cols_sb[0]:
                            st.markdown(f"**Short title:** {_b.get('short_title','')}")
                            st.markdown(f"**Blocker type:** `{_b.get('type','')}`")
                            _tbls = ", ".join(_b.get("tables") or [])
                            st.markdown(f"**Target tables:** {_tbls}")
                            st.markdown(
                                f"**Resolves in stage:** `{_b.get('resolves_in','')}`"
                            )
                        with _cols_sb[1]:
                            st.markdown(f"**What it means:** {_b.get('what_it_means','')}")
                            st.markdown(f"**What the analysis needs:** {_b.get('what_llm_needs','')}")
                            st.markdown(f"**Resolution mechanism:** {_b.get('resolves_via','')}")
                            st.markdown(f"**Analyst action now:** {_b.get('user_action_now','')}")
                        st.divider()
                    if _sb_trunc > 0:
                        st.caption(
                            f"Showing {_sb_bl_count} of "
                            f"{_sb_bl_count + _sb_trunc}; truncated per "
                            f"prompt budget."
                        )

            # ── Analyzer status grid ──
            # (script, analyzer_name, dar_analysis_type, arg_flavor)
            # arg_flavor: "singular" -> --table <t>; "plural" -> --tables <t>
            _SB_ANALYZERS: list[tuple[str, str, str, str, str]] = [
                # (cli_script, analyzer_label, dar_analysis_type, arg_flavor, note)
                ("run_completeness_analysis.py", "completeness", "completeness", "singular", ""),
                ("run_dimensions_analysis.py", "dimensions", "dimensions", "singular", ""),
                ("run_magnitude_analysis.py", "magnitude", "magnitude", "singular", ""),
                ("run_code_tables_analysis.py", "code_tables", "code_tables", "singular", ""),
                ("run_date_analysis.py", "date (temporal_coverage)", "temporal_coverage", "plural", ""),
                ("run_segmentation_analysis.py", "segmentation", "segmentation_threshold", "plural", ""),
                ("run_grain_relationship_analysis.py", "grain_relationship", "grain_relationship", "plural", ""),
                ("run_magnitude_analysis.py", "performance_baseline", "performance_baseline", "singular",
                 "Co-emitted with magnitude — clicking Run here re-runs magnitude and updates both rows."),
            ]

            # Build a status lookup keyed by analysis_type from latest DAR row
            # per (source_tables=<t>, analysis_type). Only success rows.
            try:
                _sb_dar_rows = _conn_sb.execute("""
                    SELECT analysis_type, executed_at_utc, status, result_json
                    FROM main_seeds.domain_analysis_results
                    WHERE LOWER(source_tables) = LOWER(?)
                       OR source_tables ILIKE ? OR source_tables ILIKE ?
                    ORDER BY executed_at_utc DESC
                """, [
                    _sb_selected,
                    f"{_sb_selected},%",
                    f"%,{_sb_selected}",
                ]).fetchall()
            except Exception as _sb_e:  # noqa: BLE001
                _sb_dar_rows = []
                st.warning(f"Could not load DAR history: {_sb_e}")

            _sb_latest_by_type: dict[str, tuple] = {}
            for _row in _sb_dar_rows:
                _atype, _exec_at, _status, _rjson = _row
                if _atype not in _sb_latest_by_type:
                    _sb_latest_by_type[_atype] = (_exec_at, _status, _rjson)

            # Header row.
            _hdr_cols = st.columns([2, 2, 1, 1, 1])
            _hdr_cols[0].markdown("**Analyzer**")
            _hdr_cols[1].markdown("**Last run (UTC)**")
            _hdr_cols[2].markdown("**Status**")
            _hdr_cols[3].markdown("**Blockers addressed**")
            _hdr_cols[4].markdown("**Run**")

            for _script, _label, _atype, _argf, _note in _SB_ANALYZERS:
                _last = _sb_latest_by_type.get(_atype)
                _exec_at, _status, _rjson = ("—", None, None) if _last is None else _last
                if _status == "success":
                    _status_badge = "✅ success"
                elif _status == "skipped":
                    _status_badge = "⏭️ skipped"
                elif _status == "quarantined":
                    _status_badge = "⚠️ quarantined"
                elif _status == "error":
                    _status_badge = "⚠️ error"
                else:
                    _status_badge = "⏳ never run"
                _bl_count_cell = "—"
                _skip_reason = ""
                _quarantine_caption = ""
                if _rjson:
                    try:
                        _parsed = _dj.loads(str(_rjson))
                        _ba = _parsed.get("blockers_addressed")
                        if isinstance(_ba, list):
                            _bl_count_cell = str(len(_ba))
                        if _status == "skipped":
                            _sr = _parsed.get("skip_reason")
                            if isinstance(_sr, str):
                                _skip_reason = _sr
                        if _status == "quarantined":
                            _ve = _parsed.get("validation_errors") or {}
                            if isinstance(_ve, dict):
                                _et = _ve.get("error_type") or "validation_error"
                                _ur = _ve.get("unresolved_ids") or []
                                _quarantine_caption = (
                                    f"{_et}: {len(_ur)} unresolved id(s)"
                                )
                    except Exception:  # noqa: BLE001
                        pass

                _row_cols = st.columns([2, 2, 1, 1, 1])
                _row_cols[0].markdown(f"`{_label}`")
                _row_cols[1].markdown(str(_exec_at))
                _row_cols[2].markdown(_status_badge)
                if _skip_reason:
                    _row_cols[2].caption(_skip_reason)
                elif _quarantine_caption:
                    _row_cols[2].caption(_quarantine_caption)
                _row_cols[3].markdown(_bl_count_cell)
                _run_key = f"sb_run_{_label}_{_sb_selected}"
                if _row_cols[4].button("▶ Run", key=_run_key):
                    _run_analyzer_subprocess(
                        script_rel=_script,
                        analyzer_label=_label,
                        table=_sb_selected,
                        arg_flavor=_argf,
                    )
                if _note:
                    st.caption(f"  ↳ {_note}")

                # Stage D.1: collapsible DAR results expander per cell.
                # Shows last DAR's SQL + structured results + rationale.
                # Skipped DARs render a skip_reason banner.
                if _last is not None:
                    _dar_row_for_render = {
                        "analysis_type": _atype,
                        "status": _status or "",
                        "query_sql": "",  # fetch on demand to keep table compact
                        "result_json": str(_rjson) if _rjson else "",
                        "executed_at_utc": str(_exec_at) if _exec_at else "",
                    }
                    # Fetch query_sql lazily on expander click (expensive
                    # to include up front for every cell).
                    with st.expander("▸ View results", expanded=False):
                        try:
                            _q_row = _conn_sb.execute(
                                "SELECT query_sql FROM "
                                "main_seeds.domain_analysis_results "
                                "WHERE analysis_type = ? "
                                "AND LOWER(source_tables) = LOWER(?) "
                                "ORDER BY executed_at_utc DESC LIMIT 1",
                                [_atype, _sb_selected],
                            ).fetchone()
                            if _q_row and _q_row[0]:
                                _dar_row_for_render["query_sql"] = _q_row[0]
                        except Exception:  # noqa: BLE001
                            pass
                        render_dar_card(_dar_row_for_render, st)

        # ── Stage D.1: Run All batch dispatch ──
        if _sb_selected and _sb_run_all_clicked:
            _run_all_analyzers_for_table(_sb_selected)


# ============================================================
# TAB 3: EXPLORE DATA — open-ended Q&A with knowledge reuse
# ============================================================
with tab_explore:
    st.subheader("🔍 Explore Data")
    st.caption(
        "Ask any question about your data. Claude checks prior knowledge first, "
        "generates SQL only when needed, executes against DuckDB, and saves insights."
    )

    if not qa_log.empty:
        with st.expander(
            f"📚 Knowledge base ({len(qa_log)} previous Q&A entries)",
            expanded=False,
        ):
            for _, qa in qa_log.head(10).iterrows():
                st.markdown(f"**Q:** {qa.get('question', '')}")
                st.markdown(f"**A:** {qa.get('answer_summary', '')}")
                rating = qa.get('quality_rating')
                if pd.notna(rating):
                    rating_icon = {
                        "useful": "✅",
                        "partially_useful": "⚠️",
                        "not_useful": "❌",
                    }.get(str(rating), "")
                    st.caption(f"Rating: {rating_icon} {rating}")
                st.divider()
    else:
        st.caption("Knowledge base is empty — saved answers will appear here.")

    user_question = st.text_area(
        "Ask a question about your data",
        placeholder=(
            "e.g., What's the average PO value per vendor?\n"
            "e.g., Which materials have the highest defect rate?\n"
            "e.g., Show me the distribution of movement types by plant\n"
            "e.g., Are there any vendors with declining OTD over the last 3 quarters?"
        ),
        height=100,
        key="explore_question",
    )

    if st.button(
        "🤖 Ask Claude",
        key="ask_claude_explore",
        type="primary",
        disabled=not (user_question and user_question.strip()),
    ):
        with st.spinner("Claude is thinking..."):
            from claude_api import explore_data_question

            knowledge_context = ""
            if not qa_log.empty:
                question_words = [
                    w for w in str(user_question).lower().split() if len(w) > 3
                ][:10]
                if question_words:
                    mask = qa_log.apply(
                        lambda row: any(
                            w in (str(row.get('question', '')) + str(row.get('answer_summary', ''))).lower()
                            for w in question_words
                        ),
                        axis=1,
                    )
                    relevant = qa_log[mask]
                    if not relevant.empty:
                        knowledge_context = (
                            "\n\nRelevant prior knowledge (check this FIRST before querying):\n"
                        )
                        for _, r in relevant.head(3).iterrows():
                            knowledge_context += (
                                f"Q: {r.get('question', '')}\nA: {r.get('answer_summary', '')}\n\n"
                            )

            actual_schema_text = _load_actual_staging_schema()

            mart_tables = query(
                """
                SELECT table_schema, table_name, column_name
                FROM information_schema.columns
                WHERE table_schema IN ('main_marts', 'main_obt', 'main_knowledge')
                ORDER BY table_schema, table_name, ordinal_position
                """
            ).to_csv(index=False)

            result = explore_data_question(
                question=user_question.strip(),
                actual_schema=actual_schema_text,
                mart_schema=mart_tables,
                existing_knowledge=knowledge_context,
            )

        if result and "error" not in result:
            st.session_state["explore_result"] = result
            st.session_state["explore_question_saved"] = user_question.strip()
        elif result:
            st.error(f"Claude API error: {result.get('error', 'unknown error')}")
            if 'raw_response' in result:
                with st.expander("Raw response"):
                    st.code(result['raw_response'])

    explore_result = st.session_state.get("explore_result")
    if explore_result and "error" not in explore_result:
        from_knowledge = bool(explore_result.get("from_knowledge", False))
        if from_knowledge:
            st.info("💡 Claude answered from existing knowledge — no new query executed.")

        answer = str(explore_result.get("answer", "") or "")
        sql = str(explore_result.get("sql", "") or "")
        key_facts = explore_result.get("key_facts", []) or []

        st.markdown("### Answer")
        st.markdown(answer or "_(Claude returned an empty answer)_")

        if sql:
            st.markdown("### Generated SQL")
            edited_sql = st.text_area(
                "SQL (editable)",
                value=sql,
                height=150,
                key="explore_sql",
            )
            if st.button("▶ Execute query", key="exec_explore"):
                explore_retry_count = 0
                explore_original_sql = None
                explore_final_sql = edited_sql

                with st.spinner("Executing query..."):
                    conn = get_connection()  # cached in-memory Parquet-backed
                    result_df, err = run_query_with_fallback(conn, edited_sql)

                    # If mechanical fallback failed, try LLM self-healing (silent)
                    if result_df is None and err:
                        from claude_api import llm_retry_on_error
                        result_df, fixed_sql, err, explore_retry_count, explore_original_sql = (
                            llm_retry_on_error(
                                conn,
                                rewrite_sql_for_staging(edited_sql),
                                original_question=st.session_state.get("explore_question_saved", ""),
                            )
                        )
                        if fixed_sql:
                            explore_final_sql = fixed_sql

                if result_df is not None:
                    st.markdown(f"**Results:** {len(result_df):,} rows")
                    st.dataframe(
                        result_df,
                        use_container_width=True,
                        hide_index=True,
                        height=min(400, 35 * max(1, len(result_df)) + 38),
                    )
                    if explore_retry_count > 0:
                        st.caption(f"✓ Self-corrected on attempt {explore_retry_count + 1}")
                    st.session_state["explore_result_df"] = result_df
                    # Update the SQL in explore_result so save uses the corrected version
                    if explore_final_sql != edited_sql:
                        explore_result["sql"] = explore_final_sql
                    st.session_state["explore_retry_count"] = explore_retry_count
                    st.session_state["explore_original_sql"] = explore_original_sql
                else:
                    err_lines = str(err).split("\n\nAll ")
                    display_err = err_lines[0]
                    st.error(f"Query failed: {display_err}")
                    with st.expander("Show attempted SQLs"):
                        if len(err_lines) > 1:
                            st.code(err_lines[1], language="sql")
                        else:
                            st.code(edited_sql, language="sql")

        if key_facts:
            st.markdown("### Key Facts Discovered")
            for fact in key_facts:
                st.markdown(f"- {fact}")

        st.divider()
        st.markdown("### Save to Knowledge Graph")

        col_rate, col_save = st.columns([2, 1])
        with col_rate:
            rating = st.radio(
                "Rate this answer",
                ["useful", "partially_useful", "not_useful"],
                horizontal=True,
                key="qa_rating",
            )
        with col_save:
            if st.button(
                "💾 Save Q&A to knowledge",
                key="save_qa",
                type="primary",
            ):
                csv_path = SEED_DIR / "data_qa_log.csv"
                try:
                    existing_df = pd.read_csv(csv_path)
                    max_num = (
                        existing_df['id']
                        .apply(
                            lambda x: int(x.replace('QA', ''))
                            if isinstance(x, str) and x.startswith('QA')
                            else 0
                        )
                        .max()
                        if not existing_df.empty
                        else 0
                    )
                    max_num = int(max_num or 0)
                except Exception:
                    max_num = 0

                fieldnames = [
                    'id', 'question', 'generated_sql', 'answer_summary',
                    'tables_used', 'key_facts', 'follow_up_context', 'asked_by',
                    'asked_date', 'quality_rating', 'scope', 'retry_count',
                    'original_sql',
                ]
                saved_question = st.session_state.get("explore_question_saved", "")
                qa_retry_count = st.session_state.get("explore_retry_count", 0)
                qa_original_sql = st.session_state.get("explore_original_sql")
                new_row = {
                    'id': f"QA{max_num + 1:03d}",
                    'question': saved_question,
                    'generated_sql': sql,
                    'answer_summary': answer[:500],
                    'tables_used': str(explore_result.get('tables_used', '') or ''),
                    'key_facts': '; '.join(str(k) for k in key_facts) if key_facts else '',
                    'follow_up_context': '',
                    'asked_by': 'Analyst',
                    'asked_date': pd.Timestamp.now().strftime('%Y-%m-%d'),
                    'quality_rating': rating,
                    'scope': 'qa',
                    'retry_count': str(qa_retry_count),
                    'original_sql': qa_original_sql or '',
                }
                _append_csv_row(csv_path, fieldnames, new_row)

                sync_seed(
                    "data_qa_log",
                    success_msg=(
                        "✅ Answer saved and synced. Future similar questions will "
                        "reference this entry first."
                    ),
                )
                st.session_state.pop("explore_result", None)
                st.session_state.pop("explore_question_saved", None)


# ============================================================
# TAB 4: DOMAIN REPORT — persisted LLM narrative
# ============================================================
with tab_report:
    import hashlib as _hashlib
    from pathlib import Path as _Path

    st.subheader("📝 Domain Report")

    _WIKI_DIR = _Path(__file__).resolve().parent.parent.parent / "knowledge" / "domain"
    _REPORT_CSV = SEED_DIR / "domain_reports.csv"

    all_findings = findings if not findings.empty else pd.DataFrame()
    all_qa = qa_log if not qa_log.empty else pd.DataFrame()

    # Determine domain name from findings
    _domains_covered = set()
    if not all_findings.empty and 'business_term_id' in all_findings.columns:
        for _tid in all_findings['business_term_id'].dropna().unique():
            _m = glossary[glossary['id'] == _tid]
            if not _m.empty:
                _d = _m.iloc[0].get('domain')
                if pd.notna(_d) and _d:
                    _domains_covered.add(str(_d))
    # known_issue #14 fix: stable key that doesn't drift when a new
    # domain joins the set. Previously ', '.join(sorted(...)) — every
    # new domain created a fresh row in domain_reports.csv. Now:
    # single-domain reports keyed by domain name, multi-domain reports
    # bucketed as 'cross_domain'. Orphans older multi-domain rows in
    # the CSV; acceptable (no functional regression).
    if not _domains_covered:
        _domain_name = 'general'
    elif len(_domains_covered) == 1:
        _domain_name = next(iter(_domains_covered))
    else:
        _domain_name = 'cross_domain'

    def _compute_findings_hash():
        """Hash of current findings + Q&A for staleness detection."""
        parts = []
        if not all_findings.empty:
            parts.append(all_findings.to_csv(index=False))
        if not all_qa.empty:
            parts.append(all_qa.to_csv(index=False))
        return _hashlib.sha256("".join(parts).encode()).hexdigest()[:16]

    # Check for saved report
    _report_md_path = _WIKI_DIR / f"{_domain_name.replace(', ', '_').replace(' ', '_')}_report.md"
    _saved_report_text = None
    _saved_meta = None

    try:
        _reports_df = pd.read_csv(_REPORT_CSV)
        _match = _reports_df[_reports_df['domain_name'] == _domain_name]
        if not _match.empty:
            _saved_meta = _match.iloc[0]
    except Exception:
        pass

    if _report_md_path.exists():
        try:
            _raw = _report_md_path.read_text(encoding='utf-8')
            # Strip YAML frontmatter if present
            if _raw.startswith('---'):
                _parts = _raw.split('---', 2)
                _saved_report_text = _parts[2].strip() if len(_parts) >= 3 else _raw
            else:
                _saved_report_text = _raw
        except Exception:
            pass

    # Staleness detection
    _current_hash = _compute_findings_hash()
    _current_f_count = len(all_findings)
    _current_qa_count = len(all_qa)

    _is_stale = False
    if _saved_meta is not None and _saved_report_text:
        _stored_hash = str(_saved_meta.get('findings_hash', ''))
        _raw_f = _saved_meta.get('findings_count', 0)
        _stored_f_count = int(_raw_f) if pd.notna(_raw_f) else 0
        _raw_q = _saved_meta.get('qa_count', 0)
        _stored_qa_count = int(_raw_q) if pd.notna(_raw_q) else 0
        _generated_date = str(_saved_meta.get('generated_date', ''))

        _is_stale = (
            _current_f_count != _stored_f_count
            or _current_qa_count != _stored_qa_count
            or _current_hash != _stored_hash
        )
        _new_findings = max(0, _current_f_count - _stored_f_count)

        if _is_stale:
            st.warning(
                f"Report from {_generated_date} -- "
                f"{_new_findings} new findings since last generation. "
                "Report may not reflect latest analysis."
            )
        else:
            st.success(
                f"Last generated: {_generated_date}\n\n"
                f"Based on {_stored_f_count} findings and {_stored_qa_count} Q&A entries"
            )
    elif _saved_report_text:
        st.info("Report found but metadata missing. Consider regenerating.")
    else:
        st.markdown("### No report generated yet")
        st.caption("Click below to generate a narrative domain report from your analysis findings.")

    # Generate / Regenerate button
    if _saved_report_text and _is_stale:
        _btn_label = "Refresh Domain Report"
        _btn_type = "primary"
    elif _saved_report_text:
        _btn_label = "Regenerate"
        _btn_type = "secondary"
    else:
        _btn_label = "Generate Domain Report"
        _btn_type = "primary"

    _generate_clicked = False
    if all_findings.empty and all_qa.empty:
        st.caption("No findings to generate a report from.")
    else:
        # Freshness gate: Domain Report is a write-path action — blocked on red.
        from freshness import render_freshness_banner as _ffresh_banner_dr, is_write_blocked as _is_blocked_dr
        _ffresh_banner_dr("domain_report")
        _dr_blocked = _is_blocked_dr()
        _generate_clicked = st.button(
            f"📝 {_btn_label}",
            key="gen_narrative_report",
            type=_btn_type,
            disabled=_dr_blocked,
            help=(
                "Domain facts are stale. Re-run ingestion and refresh before generating."
                if _dr_blocked else None
            ),
        )

    # Show saved report content (below button, above generation spinner)
    if _saved_report_text and not _generate_clicked:
        st.divider()
        st.markdown(_saved_report_text)

    if _generate_clicked:
        with st.spinner("Claude is writing the domain report..."):
            from claude_api import generate_domain_report

            _findings_text = (
                all_findings.to_csv(index=False) if not all_findings.empty else "No findings yet"
            )
            _qa_text = (
                all_qa.to_csv(index=False) if not all_qa.empty else "No Q&A yet"
            )
            # Archived terms are out of scope for a
            # live Domain Report — audit lives in archive_log.
            _active_gl = filter_active_terms(glossary)
            _glossary_text = _active_gl[
                ['display_name', 'definition', 'domain', 'status']
            ].to_csv(index=False)

            # Whole-domain injection — scope_tables=None so every active
            # auto_inject fact is eligible up to the 1500-token budget.
            try:
                import sys as _sys2
                from pathlib import Path as _PPath2
                _scripts2 = _PPath2(__file__).resolve().parent.parent.parent / "scripts"
                if str(_scripts2) not in _sys2.path:
                    _sys2.path.insert(0, str(_scripts2))
                from _domain_context_loader import load_domain_context as _ldc2
                _dr_domain_context = _ldc2(
                    scope_tables=None,
                    categories=None,
                    max_tokens=1500,
                    require_auto_inject=True,
                )
            except Exception:
                _dr_domain_context = ""

            _result = generate_domain_report(
                findings=_findings_text,
                qa_log=_qa_text,
                glossary=_glossary_text,
                domain_context=_dr_domain_context,
            )

        if _result and "error" not in _result:
            _narrative = _result.get("narrative", "") or ""
            _recommendations = _result.get("recommendations", []) or []
            _confidence = str(_result.get("confidence", "") or "").lower()

            # Build full markdown content
            _md_parts = [_narrative]
            if _recommendations:
                _md_parts.append("\n\n## Recommendations\n")
                for _rec in _recommendations:
                    _md_parts.append(f"- {_rec}")
            if _confidence:
                _md_parts.append(f"\n\n**Report confidence:** {_confidence.upper()}")
            _full_md = "\n".join(_md_parts)

            # Save to markdown file with YAML frontmatter
            _WIKI_DIR.mkdir(parents=True, exist_ok=True)
            _now = pd.Timestamp.now().strftime('%Y-%m-%dT%H:%M:%S')
            _terms_list = ', '.join(
                glossary[glossary['id'].isin(
                    all_findings['business_term_id'].dropna().unique()
                )]['term_name'].tolist()
            ) if not all_findings.empty and 'business_term_id' in all_findings.columns else ''

            _frontmatter = (
                f"---\n"
                f"generated: {_now}\n"
                f"findings_count: {_current_f_count}\n"
                f"qa_count: {_current_qa_count}\n"
                f"business_terms: {_terms_list}\n"
                f"---\n\n"
            )
            _report_md_path.write_text(_frontmatter + _full_md, encoding='utf-8')

            # Save metadata to domain_reports.csv
            _csv_path = _REPORT_CSV
            _fieldnames = ['domain_name', 'generated_date', 'findings_count',
                           'qa_count', 'findings_hash', 'report_status']
            _existing_rows = []
            try:
                _existing_df = pd.read_csv(_csv_path)
                _existing_rows = [
                    r for _, r in _existing_df.iterrows()
                    if r['domain_name'] != _domain_name
                ]
            except Exception:
                pass

            _new_meta = {
                'domain_name': _domain_name,
                'generated_date': _now,
                'findings_count': str(_current_f_count),
                'qa_count': str(_current_qa_count),
                'findings_hash': _current_hash,
                'report_status': 'current',
            }

            with open(_csv_path, 'w', encoding='utf-8', newline='') as _f:
                _writer = csv.DictWriter(_f, fieldnames=_fieldnames, lineterminator='\n')
                _writer.writeheader()
                for _r in _existing_rows:
                    _writer.writerow({k: _r.get(k, '') for k in _fieldnames})
                _writer.writerow(_new_meta)

            # Sync to DuckDB + Parquet
            sync_seed(
                "domain_reports",
                success_msg=(
                    f"Narrative report saved to `knowledge/domain/` and synced. "
                    f"Viewable in **Wiki Pages > Domain** tab."
                ),
            )
        elif _result:
            st.error(f"Claude API error: {_result.get('error', 'unknown error')}")
            if 'raw_response' in _result:
                with st.expander("Raw response"):
                    st.code(_result['raw_response'])
