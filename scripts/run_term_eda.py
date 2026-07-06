"""Stage C — Term EDA runner.

CLI entry: `python scripts/run_term_eda.py --term-id BG027 [--executed-by analyst]`

Three-stage trajectory:
  1. Framework floor — LLM considers all 8 Baraa lenses; picks with
     queries or skips with rationale.
  2. Reflection (mandatory once) — LLM reflects on Stage 1 results and
     emits 0-3 follow-up queries OR 'no gap identified'.
  3. Sufficiency loop (0-5 iterations) — LLM judges sufficiency;
     terminates with declared_sufficient=true OR emits 1-3 more queries.

Budget:
  - Stage 1: up to 8 queries (one per applicable lens).
  - Stage 2: 0-3 queries.
  - Stage 3: up to 5 iterations, up to 10 queries total.
  - Grand total ceiling: 21 queries per run.
  - Budget exhaustion → sufficiency row with declared_sufficient=false.

Cache discipline: system prompt + bundle cached via `_call_llm_cached`
(Stage A primitive). Per-turn dynamic suffix appends prior turns'
proposals + execution results + this turn's task.

STAGE_C_DEBUG_PROMPT_FILE env var captures the Turn-1 system+user
prompt for scenario 25's sentinel assertion.

Output: TAR rows written via `_tar_writer.write_tar_run`. Term status
transitioned on completion.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import duckdb
import requests

_ROOT = Path(__file__).resolve().parent.parent
_DB = _ROOT / "cpe_analytics.duckdb"
_ENV = _ROOT / ".env"
_PROMPT_PATH = _ROOT / "scripts" / "prompts" / "term_eda_prompt.md"
_BG_CSV = _ROOT / "dbt" / "seeds" / "business_glossary.csv"

_API_URL = "https://api.anthropic.com/v1/messages"
from _model_config import MODEL as _MODEL  # single source of truth (env: DG_AGENT_MODEL)
_MAX_TOKENS = 8000

# Lens enum per v5 Edit 2 (post-rename).
_ALL_LENSES = (
    "measures_overview", "by_dimension", "ranking", "time_trend",
    "cumulative", "variance", "bucketing", "part_to_whole",
)

# Stage 3 budget per v5 Edit 6.
_STAGE_3_MAX_ITERATIONS = 5
_STAGE_3_MAX_QUERIES = 10

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tar_writer import (  # noqa: E402
    build_query_row, build_sufficiency_row, write_tar_run,
    peek_next_tar_ids, validate_query_grounded_in_tar_ids,
)
from _tar_corpus_loader import load_candidate_prior_tars  # noqa: E402
from _term_eda_prereq import check_term_eda_prereq  # noqa: E402


# ─── env + prompt loading ──────────────────────────────────────────────

def _load_env() -> None:
    if not _ENV.exists():
        return
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _load_prompt() -> str:
    """Return the full system prompt — context + 8-lens framework +
    status definitions + SQL constraints + ALL per-turn JSON schemas.

    The whole document is the cacheable prefix; per-turn user prompts
    reference the turn-specific schema by name. Without the per-turn
    TURN sections included, the LLM never sees the exact JSON shape it
    should emit (bug caught in scenario 25's first run)."""
    raw = _PROMPT_PATH.read_text(encoding="utf-8")
    sys_marker = "## SYSTEM PROMPT"
    if sys_marker not in raw:
        raise RuntimeError(f"Prompt template malformed: {_PROMPT_PATH}")
    start = raw.index(sys_marker) + len(sys_marker)
    return raw[start:].strip()


# ─── LLM primitive (mirrors Stage A _call_llm_cached exactly) ──────────

def _call_llm_cached(system_prompt: str, user_prompt: str,
                     api_key: str) -> dict:
    """Single cached LLM call. Stage C invokes this once per turn; cache
    amortizes across all turns of a run."""
    r = requests.post(
        _API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": _MODEL,
            "max_tokens": _MAX_TOKENS,
            "system": [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": [{
                "role": "user",
                "content": [{"type": "text", "text": user_prompt}],
            }],
        },
        timeout=300,
    )
    r.raise_for_status()
    body = r.json()
    text = body["content"][0]["text"].strip()
    if text.startswith("```"):
        lines = text.splitlines()
        end = len(lines) if not lines[-1].startswith("```") else -1
        text = "\n".join(lines[1:end])
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        # The model sometimes wraps the JSON in prose plus a fenced block
        # ("Looking at the gaps...\n```json\n{...}\n```") — the startswith
        # strip above never fires then. Extract the fenced block, or fall
        # back to the outermost brace span, before giving up.
        fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else None
        if candidate is None:
            start, end_i = text.find("{"), text.rfind("}")
            if start != -1 and end_i > start:
                candidate = text[start:end_i + 1]
        if candidate is not None:
            try:
                payload = json.loads(candidate)
                return {"payload": payload, "usage": body.get("usage", {})}
            except json.JSONDecodeError:
                pass
        raise RuntimeError(
            f"LLM response not valid JSON: {e}. Tail: {text[-400:]!r}"
        ) from e
    return {"payload": payload, "usage": body.get("usage", {})}


# ─── bundle rendering ──────────────────────────────────────────────────

def _load_term(conn, term_id: str) -> dict:
    row = conn.execute(
        """
        SELECT id, term_name, display_name, definition, unit, grain,
               domain, notes, business_join_description,
               business_filter_description, status,
               COALESCE(scope_derivation_history_json, '{}') AS history
        FROM main_seeds.business_glossary WHERE id = ?
        """,
        [term_id],
    ).fetchone()
    if not row:
        raise ValueError(f"term {term_id} not found in business_glossary")
    keys = ["id", "term_name", "display_name", "definition", "unit",
            "grain", "domain", "notes", "business_join_description",
            "business_filter_description", "status", "history"]
    return dict(zip(keys, row))


def _load_scope_tables(conn, term_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT LOWER(source_table) FROM main_seeds.s2t_mapping "
        "WHERE business_term_id = ? ORDER BY 1",
        [term_id],
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def _load_dar_summaries(conn, scope_tables: list[str]) -> list[dict]:
    """Fetch latest-success DARs for each scope table, one per table per
    analysis_type. Returns compact dicts for bundle rendering."""
    out: list[dict] = []
    for t in scope_tables:
        rows = conn.execute(
            """
            SELECT id, analysis_type, executed_at_utc, result_json
            FROM main_seeds.domain_analysis_results
            WHERE LOWER(source_tables) = LOWER(?)
              AND status = 'success'
            ORDER BY executed_at_utc DESC
            """,
            [t],
        ).fetchall()
        seen_types: set[str] = set()
        for row in rows:
            dar_id, atype, exec_at, result_json = row
            if atype in seen_types:
                continue
            seen_types.add(atype)
            out.append({
                "id": dar_id,
                "source_table": t,
                "analysis_type": atype,
                "executed_at_utc": str(exec_at),
                "result_json": result_json or "{}",
            })
    return out


def _load_stage_a_blockers(term_history_json: str) -> list[dict]:
    """Extract confirmed-iteration blockers from scope_derivation_history_json."""
    try:
        history = json.loads(term_history_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    iterations = history.get("iterations") or []
    # Prefer the confirmed iteration; fall back to final_iter_num.
    confirmed: Optional[dict] = None
    for it in iterations:
        if isinstance(it, dict) and it.get("analyst_action") == "confirmed":
            confirmed = it
            break
    if confirmed is None:
        final_idx = history.get("final_iter_num")
        if final_idx is not None:
            try:
                confirmed = iterations[int(final_idx) - 1]
            except (IndexError, TypeError, ValueError):
                confirmed = None
    if not isinstance(confirmed, dict):
        return []
    resp = confirmed.get("llm_response") or {}
    blockers = resp.get("blockers") or []
    return [b for b in blockers if isinstance(b, dict)]


def _load_catalog_for_scope(conn, scope_tables: list[str]) -> str:
    """Minimal column catalog for scope tables."""
    if not scope_tables:
        return ""
    placeholders = ",".join("?" * len(scope_tables))
    rows = conn.execute(
        f"""
        SELECT LOWER(table_name) AS t, field_name, data_type, length,
               COALESCE(description_en, '') AS descr
        FROM main_seeds.sap_data_dictionary
        WHERE LOWER(table_name) IN ({placeholders})
        ORDER BY 1, 2
        """,
        scope_tables,
    ).fetchall()
    lines = [f"{t}.{f} [{dt_ or '?'}{('/'+str(ln)) if ln else ''}] {descr}"
             for t, f, dt_, ln, descr in rows]
    return "\n".join(lines)


def _render_bundle(
    term: dict,
    scope_tables: list[str],
    dars: list[dict],
    prior_tars: list[dict],
    blockers: list[dict],
    catalog: str,
) -> str:
    """Format the full cacheable bundle prefix appended to every turn's
    user prompt. Content is per-run stable; the dynamic per-turn task
    suffix is appended after this."""
    parts: list[str] = []
    parts.append("# Bundle for Stage C Term EDA")
    parts.append("")

    # Term attributes.
    parts.append("## Term")
    parts.append(f"- id: {term['id']}")
    parts.append(f"- term_name: {term['term_name']}")
    parts.append(f"- display_name: {term.get('display_name') or ''}")
    parts.append(f"- definition: {term.get('definition') or ''}")
    parts.append(f"- grain: {term.get('grain') or ''}")
    parts.append(f"- unit: {term.get('unit') or ''}")
    parts.append(f"- domain: {term.get('domain') or ''}")
    if term.get("notes"):
        parts.append(f"- notes: {term['notes']}")
    if term.get("business_join_description"):
        parts.append(f"- business_join_description: {term['business_join_description']}")
    if term.get("business_filter_description"):
        parts.append(f"- business_filter_description: {term['business_filter_description']}")
    parts.append("")

    # Scope.
    parts.append("## Scope tables (from s2t_mapping)")
    parts.append(", ".join(scope_tables) if scope_tables else "(empty)")
    parts.append("")

    # DARs.
    parts.append("## Domain EDA evidence (DARs, latest success per analysis_type per scope table)")
    if dars:
        for d in dars:
            parts.append(
                f"- {d['id']} | {d['source_table']} | {d['analysis_type']} "
                f"| {d['executed_at_utc']}"
            )
            # Include trimmed result_json (first 800 chars) for evidence content.
            rj = (d.get("result_json") or "")[:800]
            parts.append(f"  result_json (first 800 chars): {rj}")
    else:
        parts.append("(no DARs for scope — Stage C prereqs should have blocked this)")
    parts.append("")

    # Prior TARs.
    parts.append("## Prior TAR candidates (cross-term knowledge reuse)")
    if prior_tars:
        for p in prior_tars:
            note = ""
            if p.get("superseded_flag"):
                succ = p.get("current_successor_id")
                succ_part = f" (current successor: {succ})" if succ else ""
                note = (
                    f"\n  [CITATION NOTE: TAR {p['id']} is superseded; "
                    f"historical evidence only{succ_part}]"
                )
            parts.append(
                f"- {p['id']} | term={p['term_id']} "
                f"({p.get('originating_term_name') or ''}) "
                f"| lens={p.get('analysis_lens') or ''} "
                f"| stage={p.get('stage') or ''} | "
                f"result_rows={p.get('result_row_count', 0)}{note}"
            )
            interp = (p.get("interpretation") or "")[:200]
            if interp:
                parts.append(f"  interpretation: {interp}")
            sql = (p.get("query_sql") or "")[:300]
            if sql:
                parts.append(f"  sql (first 300 chars): {sql}")
    else:
        parts.append("(no prior TARs overlap this term's scope)")
    parts.append("")

    # Blockers.
    parts.append("## Stage A blockers attached to this term (confirmed iteration)")
    if blockers:
        for i, b in enumerate(blockers, 1):
            parts.append(f"- Blocker {i}:")
            parts.append(f"  - short_title: {b.get('short_title', '')}")
            parts.append(f"  - type: {b.get('type', '')}")
            parts.append(f"  - tables: {b.get('tables', [])}")
            parts.append(f"  - what_it_means: {b.get('what_it_means', '')}")
            parts.append(f"  - what_llm_needs: {b.get('what_llm_needs', '')}")
            parts.append(f"  - resolves_in: {b.get('resolves_in', '')}")
            parts.append(f"  - resolves_via: {b.get('resolves_via', '')}")
            parts.append(f"  - user_action_now: {b.get('user_action_now', '')}")
    else:
        parts.append("(no blockers attached to this term)")
    parts.append("")

    # Catalog (column-level for scope tables).
    parts.append("## SAP column catalog (scope tables only)")
    parts.append(catalog or "(catalog empty)")
    parts.append("")

    return "\n".join(parts)


# ─── query execution ──────────────────────────────────────────────────

_STG_REF_PATTERN = re.compile(
    r"\braw_sap\.([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _rewrite_raw_sap_refs(sql: str) -> str:
    """Defensive: LLM may emit raw_sap.<t> despite the constraint. Rewrite
    to main_staging.stg_sap__<t> so executions don't fail on staging-only
    deployments. Mirrors the rewrite_sql_for_staging pattern in the
    Streamlit query path."""
    return _STG_REF_PATTERN.sub(
        lambda m: f"main_staging.stg_sap__{m.group(1).lower()}",
        sql,
    )


def _execute_query(conn, sql: str, max_rows: int = 100) -> dict:
    """Execute a single SELECT and return {status, result_json, row_count,
    error}. Never raises — execution errors become status='error' rows.
    """
    try:
        rewritten = _rewrite_raw_sap_refs(sql)
        result = conn.execute(rewritten).fetchall()
        cols = [d[0] for d in conn.description] if conn.description else []
        truncated = result[:max_rows]
        rows_as_dicts = [dict(zip(cols, row)) for row in truncated]
        return {
            "status": "success",
            "result_json": json.dumps(rows_as_dicts, default=str),
            "row_count": len(result),
            "error": "",
        }
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "result_json": "",
            "row_count": 0,
            "error": f"{type(e).__name__}: {e}",
        }


# ─── status transition ─────────────────────────────────────────────────

def _update_term_status(term_id: str, new_status: str) -> None:
    """Rewrite business_glossary.csv to set term_id's status. LF-only,
    atomic via .tmp + os.replace. Mirrors Stage A / Stage B status-
    transition pattern. Triggers re-seed + parquet sync on success.
    """
    with _BG_CSV.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        heads = reader.fieldnames
    found = False
    for row in rows:
        if row.get("id") == term_id:
            row["status"] = new_status
            found = True
            break
    if not found:
        raise ValueError(f"term {term_id} not found in business_glossary.csv")
    tmp = _BG_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f, fieldnames=heads, lineterminator="\n",
            quoting=csv.QUOTE_MINIMAL,
        )
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in heads})
    os.replace(tmp, _BG_CSV)

    # Best-effort reseed + parquet sync.
    try:
        from _parquet_sync import sync_parquet_and_invalidate
        sync_parquet_and_invalidate(
            project_root=_ROOT,
            seed_name="business_glossary",
            skip=False,
            source="run_term_eda.update_term_status",
        )
    except Exception as e:  # noqa: BLE001
        print(
            f"[WARN] run_term_eda: status-transition parquet sync failed: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )


# ─── usage accumulation ────────────────────────────────────────────────

def _accumulate_usage(acc: dict, turn_usage: dict) -> None:
    for k in ("input_tokens", "output_tokens",
              "cache_creation_input_tokens", "cache_read_input_tokens"):
        acc[k] = acc.get(k, 0) + int(turn_usage.get(k, 0) or 0)


# ─── main orchestrator ────────────────────────────────────────────────

def _maybe_write_debug_prompt(system_prompt: str, user_prompt: str) -> None:
    path = os.environ.get("STAGE_C_DEBUG_PROMPT_FILE")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("# SYSTEM PROMPT\n\n")
            f.write(system_prompt)
            f.write("\n\n# USER PROMPT (Turn 1)\n\n")
            f.write(user_prompt)
    except OSError as e:
        print(
            f"[WARN] STAGE_C_DEBUG_PROMPT_FILE write failed: {e}",
            file=sys.stderr,
        )


def _validate_lens_decisions(lens_decisions: dict) -> list[str]:
    issues: list[str] = []
    missing = set(_ALL_LENSES) - set(lens_decisions.keys())
    if missing:
        issues.append(f"missing lenses: {sorted(missing)}")
    for lens, entry in lens_decisions.items():
        if not isinstance(entry, dict):
            issues.append(f"lens {lens}: not a dict")
            continue
        dec = entry.get("decision")
        if dec not in ("picked", "skipped"):
            issues.append(f"lens {lens}: decision={dec!r} invalid")
        qs = entry.get("queries") or []
        tars = entry.get("cite_tar_ids") or []
        if dec == "picked" and not qs and not tars:
            issues.append(
                f"lens {lens}: picked but no queries and no citations"
            )
    return issues


def _construct_lens_consideration(
    llm_lens_consideration: dict,
    lens_decisions: dict,
    all_query_rows: list[dict],
    query_ids: list[str],
) -> dict:
    """Server-side construction of sufficiency_json.lens_consideration.

    Replaces LLM-emitted tar_ids with runner-assembled citations. For
    each lens, tar_ids = (this run's query rows whose analysis_lens
    matches) ∪ (prior TAR ids the LLM cited for this lens at
    framework_floor). Decision + rationale come from the LLM's terminal
    output where present, falling back to the framework_floor decision.

    Inverts the responsibility model: the LLM owns semantic judgment
    (which lens is picked, why); the runner owns id assembly. The LLM
    never emits tar_id strings, so it cannot hallucinate them.
    """
    lc: dict = {}
    for lens in _ALL_LENSES:
        run_tar_ids = [
            qid for qrow, qid in zip(all_query_rows, query_ids)
            if qrow.get("analysis_lens") == lens
        ]
        ff = lens_decisions.get(lens) or {}
        cite_tar_ids = [
            t for t in (ff.get("cite_tar_ids") or [])
            if isinstance(t, str)
        ]
        all_tar_ids = sorted(set(run_tar_ids) | set(cite_tar_ids))
        terminal = llm_lens_consideration.get(lens)
        if not isinstance(terminal, dict):
            terminal = {}
        lc[lens] = {
            "decision": (
                terminal.get("decision") or ff.get("decision", "skipped")
            ),
            "rationale": (
                terminal.get("rationale") or ff.get("rationale", "")
            ),
            "tar_ids": all_tar_ids,
        }
    return lc


def run_term_eda(term_id: str, executed_by: str = "system") -> dict:
    """Execute the full Stage C trajectory for one term. Returns a
    result dict for caller consumption."""
    t0 = time.perf_counter()
    _load_env()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "status": "error",
            "term_id": term_id,
            "error": "ANTHROPIC_API_KEY not set",
            "elapsed_seconds": 0.0,
        }

    conn = duckdb.connect(str(_DB), read_only=False)
    usage_acc: dict = {}
    try:
        # Prerequisites.
        prereq = check_term_eda_prereq(conn, term_id)
        if not prereq["ready"]:
            return {
                "status": "error",
                "term_id": term_id,
                "error": f"prerequisites not met: {prereq['reason']}",
                "missing_dar_tables": sorted(
                    prereq.get("missing_analyzers_per_table", {}).keys()
                ),
                "missing_grain_pairs": prereq.get("missing_grain_pairs", []),
                "next_steps": prereq["next_steps"],
                "elapsed_seconds": time.perf_counter() - t0,
            }

        # Load all bundle inputs.
        term = _load_term(conn, term_id)
        scope_tables = _load_scope_tables(conn, term_id)
        dars = _load_dar_summaries(conn, scope_tables)
        prior_tars = load_candidate_prior_tars(conn, term_id)
        blockers = _load_stage_a_blockers(term.get("history") or "{}")
        catalog = _load_catalog_for_scope(conn, scope_tables)

        system_prompt = _load_prompt()
        bundle = _render_bundle(term, scope_tables, dars, prior_tars,
                                blockers, catalog)

        # Transition term status BEFORE running. If the caller aborts,
        # the term stays in term_eda_pending — correct per D7 row 3.
        original_status = term["status"]
        try:
            conn.close()  # release write lock for CSV-level rewrite
            _update_term_status(term_id, "term_eda_pending")
        finally:
            conn = duckdb.connect(str(_DB), read_only=False)

        # ─── STAGE 1 — Framework Floor ───
        turn1_task = (
            "## Bundle\n\n" + bundle + "\n\n## Task for this turn\n\n"
            "Turn 1 — Framework Floor. Consider all 8 lenses. Emit the "
            "JSON schema specified under `## TURN 1 — FRAMEWORK FLOOR` "
            "in the system prompt. Return ONLY the JSON object."
        )
        _maybe_write_debug_prompt(system_prompt, turn1_task)

        try:
            resp1 = _call_llm_cached(system_prompt, turn1_task, api_key)
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "term_id": term_id,
                "error": f"Turn 1 failed: {type(e).__name__}: {e}",
                "elapsed_seconds": time.perf_counter() - t0,
            }
        _accumulate_usage(usage_acc, resp1["usage"])
        lens_decisions = (resp1["payload"] or {}).get("lens_decisions") or {}
        issues = _validate_lens_decisions(lens_decisions)
        if issues:
            return {
                "status": "error",
                "term_id": term_id,
                "error": f"Turn 1 validation: {issues}",
                "elapsed_seconds": time.perf_counter() - t0,
            }

        # Collect Stage 1 queries + execute.
        query_index = 0
        all_query_rows: list[dict] = []
        for lens in _ALL_LENSES:
            entry = lens_decisions[lens]
            if entry["decision"] != "picked":
                continue
            for q in entry.get("queries") or []:
                query_index += 1
                sql = str(q.get("query_sql") or "").strip()
                if not sql:
                    continue
                exec_result = _execute_query(conn, sql)
                all_query_rows.append(build_query_row(
                    term_id=term_id,
                    analysis_lens=lens,
                    stage="framework_floor",
                    query_index=query_index,
                    query_sql=sql,
                    query_result_json=exec_result["result_json"],
                    result_row_count=exec_result["row_count"],
                    interpretation=str(q.get("query_explanation") or "").strip(),
                    grounded_in_tar_ids=list(q.get("grounded_in_tar_ids") or []),
                    status=exec_result["status"],
                    error_message=exec_result.get("error", ""),
                ))

        # ─── STAGE 2 — Reflection ───
        turn2_task = (
            "## Bundle (repeated for cache prefix)\n\n" + bundle +
            "\n\n## Turn 1 lens_decisions\n\n" +
            json.dumps(lens_decisions, default=str, indent=2) +
            "\n\n## Turn 1 query execution results\n\n" +
            _render_query_results(all_query_rows) +
            "\n\n## Task for this turn\n\n"
            "Turn 2 — Reflection. Given Turn 1 evidence and all Stage A "
            "blockers, identify the ONE gap that most improves S2T "
            "confidence. Emit the JSON schema specified under `## TURN 2 "
            "— REFLECTION` in the system prompt."
        )

        try:
            resp2 = _call_llm_cached(system_prompt, turn2_task, api_key)
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "term_id": term_id,
                "error": f"Turn 2 failed: {type(e).__name__}: {e}",
                "elapsed_seconds": time.perf_counter() - t0,
            }
        _accumulate_usage(usage_acc, resp2["usage"])
        turn2_payload = resp2["payload"] or {}
        reflection_summary = str(turn2_payload.get("reflection_summary") or "")
        for q in turn2_payload.get("follow_up_queries") or []:
            query_index += 1
            sql = str(q.get("query_sql") or "").strip()
            if not sql:
                continue
            lens = str(q.get("lens") or "").strip() or "measures_overview"
            if lens not in _ALL_LENSES:
                lens = "measures_overview"
            exec_result = _execute_query(conn, sql)
            all_query_rows.append(build_query_row(
                term_id=term_id,
                analysis_lens=lens,
                stage="reflection",
                query_index=query_index,
                query_sql=sql,
                query_result_json=exec_result["result_json"],
                result_row_count=exec_result["row_count"],
                interpretation=str(q.get("query_explanation") or "").strip(),
                grounded_in_tar_ids=list(q.get("grounded_in_tar_ids") or []),
                status=exec_result["status"],
                error_message=exec_result.get("error", ""),
            ))

        # ─── STAGE 3 — Sufficiency Loop ───
        stage_3_iterations = 0
        stage_3_queries = 0
        declared_sufficient = False
        sufficiency_rationale = ""
        budget_exhausted = False

        while stage_3_iterations < _STAGE_3_MAX_ITERATIONS \
                and stage_3_queries < _STAGE_3_MAX_QUERIES:
            stage_3_iterations += 1
            turn3_task = (
                "## Bundle (repeated for cache prefix)\n\n" + bundle +
                "\n\n## Turn 1 lens_decisions\n\n" +
                json.dumps(lens_decisions, default=str, indent=2) +
                "\n\n## Turn 2 reflection\n\n" +
                json.dumps(turn2_payload, default=str, indent=2) +
                f"\n\n## Stage 3 iteration {stage_3_iterations} of max "
                f"{_STAGE_3_MAX_ITERATIONS}\n\n"
                "## All query execution results so far\n\n" +
                _render_query_results(all_query_rows) +
                "\n\n## Task for this turn\n\n"
                "Turn 3 — Sufficiency judgment. Decide whether existing "
                "evidence is sufficient for confident S2T authoring. "
                "Emit the JSON schema specified under `## TURN 3+ — "
                "SUFFICIENCY LOOP` in the system prompt. If "
                "declared_sufficient=true, set more_queries=[]."
            )

            try:
                resp3 = _call_llm_cached(system_prompt, turn3_task, api_key)
            except Exception as e:  # noqa: BLE001
                return {
                    "status": "error",
                    "term_id": term_id,
                    "error": (
                        f"Turn 3 iter {stage_3_iterations} failed: "
                        f"{type(e).__name__}: {e}"
                    ),
                    "elapsed_seconds": time.perf_counter() - t0,
                }
            _accumulate_usage(usage_acc, resp3["usage"])
            payload3 = resp3["payload"] or {}
            declared_sufficient = bool(payload3.get("declared_sufficient"))
            sufficiency_rationale = str(payload3.get("sufficiency_rationale") or "")

            if declared_sufficient:
                break

            for q in payload3.get("more_queries") or []:
                if stage_3_queries >= _STAGE_3_MAX_QUERIES:
                    budget_exhausted = True
                    break
                query_index += 1
                stage_3_queries += 1
                sql = str(q.get("query_sql") or "").strip()
                if not sql:
                    continue
                lens = str(q.get("lens") or "").strip() or "measures_overview"
                if lens not in _ALL_LENSES:
                    lens = "measures_overview"
                exec_result = _execute_query(conn, sql)
                all_query_rows.append(build_query_row(
                    term_id=term_id,
                    analysis_lens=lens,
                    stage="sufficiency_loop",
                    query_index=query_index,
                    query_sql=sql,
                    query_result_json=exec_result["result_json"],
                    result_row_count=exec_result["row_count"],
                    interpretation=str(q.get("query_explanation") or "").strip(),
                    grounded_in_tar_ids=list(q.get("grounded_in_tar_ids") or []),
                    status=exec_result["status"],
                    error_message=exec_result.get("error", ""),
                ))
            if budget_exhausted:
                break

        if not declared_sufficient and stage_3_iterations >= _STAGE_3_MAX_ITERATIONS:
            budget_exhausted = True

        # ─── TERMINAL — Sufficiency Payload ───
        terminal_task_base = (
            "## Bundle (repeated for cache prefix)\n\n" + bundle +
            "\n\n## Turn 1 lens_decisions\n\n" +
            json.dumps(lens_decisions, default=str, indent=2) +
            "\n\n## Turn 2 reflection\n\n" +
            json.dumps(turn2_payload, default=str, indent=2) +
            "\n\n## All query execution results\n\n" +
            _render_query_results(all_query_rows) +
            f"\n\n## Stage 3 summary: {stage_3_iterations} iterations, "
            f"{stage_3_queries} queries, declared_sufficient="
            f"{declared_sufficient}, budget_exhausted={budget_exhausted}\n\n"
            "## Task for this turn\n\n"
            "Terminal — Emit the final sufficiency payload JSON per "
            "`## TERMINAL — SUFFICIENCY PAYLOAD` in the system prompt. "
            "Include lens_consideration (all 8 lenses), reflection_summary, "
            "sufficiency_loop_iterations, declared_sufficient, "
            "sufficiency_rationale, confidence (high|medium|low), and "
            "blockers_resolution (one entry per Stage A blocker)."
        )

        # Single terminal LLM call. The LLM emits semantic content only
        # (decision + rationale per lens, blockers_resolution, etc.); the
        # runner constructs lens_consideration[lens].tar_ids server-side
        # from this run's allocated query ids. Eliminates the tar_id
        # hallucination class structurally.
        try:
            resp_terminal = _call_llm_cached(
                system_prompt, terminal_task_base, api_key,
            )
        except Exception as e:  # noqa: BLE001
            return {
                "status": "error",
                "term_id": term_id,
                "error": f"Terminal turn failed: {type(e).__name__}: {e}",
                "elapsed_seconds": time.perf_counter() - t0,
            }
        _accumulate_usage(usage_acc, resp_terminal["usage"])
        final_payload = resp_terminal["payload"] or {}

        # Merge runner-known truth into final payload (source of truth
        # for these fields is the runner, not the LLM — guards against
        # the LLM reporting stale counts).
        final_payload["sufficiency_loop_iterations"] = stage_3_iterations
        if budget_exhausted and not declared_sufficient:
            final_payload["declared_sufficient"] = False
            prior_rationale = final_payload.get("sufficiency_rationale") or ""
            final_payload["sufficiency_rationale"] = (
                "budget_exhausted_stage_3: "
                + (prior_rationale or "reached Stage 3 budget without sufficiency")
            )
        else:
            final_payload["declared_sufficient"] = declared_sufficient
            if not final_payload.get("sufficiency_rationale"):
                final_payload["sufficiency_rationale"] = sufficiency_rationale

        if final_payload.get("confidence") not in ("high", "medium", "low"):
            final_payload["confidence"] = "low" if not declared_sufficient else "medium"

        # Server-side lens_consideration construction. Peek the ids that
        # write_tar_run will allocate for this run; map this-run query
        # rows + framework_floor cite_tar_ids to per-lens citations. LLM
        # output's tar_ids field (if present) is ignored.
        _, query_ids_pre = peek_next_tar_ids(len(all_query_rows))
        final_payload["lens_consideration"] = _construct_lens_consideration(
            llm_lens_consideration=final_payload.get("lens_consideration") or {},
            lens_decisions=lens_decisions,
            all_query_rows=all_query_rows,
            query_ids=query_ids_pre,
        )

        final_payload.setdefault("reflection_summary", reflection_summary)
        final_payload.setdefault("blockers_resolution", [])

        # Also capture LLM-generated per-query interpretations if it
        # emitted them alongside the terminal payload (optional).
        interpretations_map = final_payload.get("interpretations") or {}

        # Build sufficiency row.
        sufficiency_row = build_sufficiency_row(
            term_id=term_id,
            sufficiency_json=final_payload,
            confidence=final_payload["confidence"],
            query_index=query_index + 1,
            grounded_in_tar_ids=[],  # row-level citations on query rows
            status="success",
            llm_usage_json=json.dumps(usage_acc, default=str),
        )

        # Apply LLM-side interpretations onto query rows when provided.
        if isinstance(interpretations_map, dict) and interpretations_map:
            for qrow in all_query_rows:
                key = str(qrow.get("query_index", ""))
                new_interp = interpretations_map.get(key)
                if new_interp and not qrow.get("interpretation"):
                    qrow["interpretation"] = str(new_interp)

        # KI-111 — validate grounded_in_tar_ids citations against the
        # bundle's candidate_prior_tar_ids. Cited ids should belong to
        # the cross-term knowledge-reuse pool surfaced via
        # load_candidate_prior_tars; LLM-emitted ids outside that pool
        # are hallucinations. Strip them before persisting and log the
        # violation for audit. Same fix-class as KI-109; sibling
        # representation (per-query) that KI-109's phase-restructure
        # intentionally deferred. Strip rather than raise — analysis
        # itself already happened, only the advisory citations are bad.
        candidate_prior_tar_id_set = [
            (t.get("id") or "") for t in (prior_tars or []) if t.get("id")
        ]
        ok_ground, ground_errors = validate_query_grounded_in_tar_ids(
            all_query_rows, candidate_prior_tar_id_set,
        )
        if not ok_ground:
            print(
                f"WARN: KI-111 — stripping hallucinated grounded_in_tar_ids "
                f"from {len(ground_errors['violations'])} query row(s); "
                f"unresolved ids cited but not in bundle: "
                f"{json.dumps(ground_errors, default=str)[:600]}",
                file=sys.stderr,
            )
            candidate_set = set(candidate_prior_tar_id_set)
            for qrow in all_query_rows:
                cited = qrow.get("grounded_in_tar_ids") or []
                qrow["grounded_in_tar_ids"] = [
                    t for t in cited if t in candidate_set
                ]

        # ─── Write TARs atomically ───
        run_id, new_ids = write_tar_run(
            term_id=term_id,
            query_rows=all_query_rows,
            sufficiency_row=sufficiency_row,
            executed_by=executed_by,
        )

        # ─── Compute final term status per v4 D7 + v5 Edit 10 ───
        blockers_res = final_payload.get("blockers_resolution") or []
        has_could_not_resolve = any(
            isinstance(br, dict) and br.get("status") == "could_not_resolve"
            for br in blockers_res
        )
        has_escalation = any(
            isinstance(br, dict) and br.get("status") == "escalated_to_analyst"
            for br in blockers_res
        )

        if (final_payload["declared_sufficient"]
                and not has_could_not_resolve
                and not has_escalation):
            final_term_status = "ready_for_s2t"
            # Close connection before CSV rewrite (re-seed uses
            # subprocess dbt, which needs its own writer lock).
            conn.close()
            _update_term_status(term_id, "ready_for_s2t")
        else:
            final_term_status = "term_eda_pending"
            # Already transitioned above; no further change needed.

        return {
            "status": "success",
            "term_id": term_id,
            "run_id": run_id,
            "query_row_count": len(all_query_rows),
            "declared_sufficient": final_payload["declared_sufficient"],
            "confidence": final_payload["confidence"],
            "stage_3_iterations": stage_3_iterations,
            "stage_3_queries": stage_3_queries,
            "budget_exhausted": budget_exhausted,
            "blockers_resolution_summary": [
                {
                    "short_title": (br.get("blocker_short_title") if isinstance(br, dict) else None),
                    "status": (br.get("status") if isinstance(br, dict) else None),
                }
                for br in blockers_res
            ],
            "final_term_status": final_term_status,
            "llm_usage": usage_acc,
            "elapsed_seconds": time.perf_counter() - t0,
        }

    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _render_query_results(query_rows: list[dict]) -> str:
    """Render executed query rows compactly for next-turn bundle inclusion.

    Includes the captured error_message (KI-113) on status='error' rows
    so the LLM has the negative-feedback signal needed to self-correct
    binder errors instead of blindly retrying the same wrong pattern.
    """
    if not query_rows:
        return "(no queries executed yet)"
    parts = []
    for q in query_rows:
        parts.append(
            f"- query_index={q.get('query_index')} | "
            f"lens={q.get('analysis_lens')} | "
            f"stage={q.get('stage')} | "
            f"status={q.get('status')} | "
            f"result_rows={q.get('result_row_count')}"
        )
        sql = (q.get("query_sql") or "")[:400]
        parts.append(f"  sql: {sql}")
        err = (q.get("error_message") or "").strip()
        if err:
            parts.append(f"  error_message: {err[:600]}")
        rj = (q.get("query_result_json") or "")[:800]
        if rj:
            parts.append(f"  result_json (first 800 chars): {rj}")
    return "\n".join(parts)


# ─── CLI entry ────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage C — Term EDA runner.",
    )
    parser.add_argument("--term-id", required=True)
    parser.add_argument("--executed-by", default="system")
    args = parser.parse_args(argv)

    result = run_term_eda(
        term_id=args.term_id,
        executed_by=args.executed_by,
    )
    print(json.dumps(result, default=str, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
